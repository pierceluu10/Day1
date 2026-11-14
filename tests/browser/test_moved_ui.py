"""Track Q3 drill: scripts/move_ui.sh really changes Gitea's SSH-key form underneath saved locators.

Runs only while the moved template is installed (scripts/move_ui.sh, undone by scripts/restore_ui.sh); it is
skipped otherwise. The form's fields get new ids and swap order, so the key name and content locators saved
by earlier runs fail: red "This moved.", then reasoning (amber), the key is added and verified by the API and
`ssh -T`, and the new locators are saved. A second run uses them from memory (green) with no LLM call.
Honours BROWSER_HEADLESS so it can be watched headed.
"""

from __future__ import annotations

import subprocess

import pytest

from dayone.browser.locators import KEYS_CONTENT, KEYS_OPEN_FORM, KEYS_SUBMIT, KEYS_TITLE
from dayone.config import settings
from dayone.contracts import StepKind
from dayone.memory.experience import EXPERIENCES

from conftest import HIRE, locator_events, reset_dev_hire

GITEA_CONTAINER = "gitea"
MOVED_TEMPLATE = "/data/gitea/templates/user/settings/keys_ssh.tmpl"  # where scripts/move_ui.sh installs it
MOVED_FIELDS = (KEYS_TITLE, KEYS_CONTENT)
UNMOVED = (KEYS_OPEN_FORM, KEYS_SUBMIT)


def ui_is_moved() -> bool:
    probe = subprocess.run(["docker", "exec", GITEA_CONTAINER, "test", "-e", MOVED_TEMPLATE], capture_output=True)
    return probe.returncode == 0


def remembered(db, goal: str) -> dict:
    return db[EXPERIENCES].find_one({"kind": StepKind.BROWSER, "outcome": "verified", "goal": goal}, sort=[("updated_at", -1)])


@pytest.mark.skipif(not ui_is_moved(), reason="run scripts/move_ui.sh first; restore with scripts/restore_ui.sh")
def test_move_ui_is_recovered_by_reasoning_then_remembered(clean_hire, db, atlas_runner, gitea, container):
    before = {goal: remembered(db, goal) for goal in MOVED_FIELDS + UNMOVED}
    print("phase 1, saved locators:", {goal: doc and (doc["locator"], doc["hire"]) for goal, doc in before.items()})
    assert all(before.values()), "no saved locators for the key form: run tests/browser once on the stock UI first"
    headless = settings().browser_headless

    first_run = atlas_runner.run(headless=headless)
    first = {goal: locator_events(db, first_run, goal) for goal in MOVED_FIELDS + UNMOVED}
    print("run 1 LOCATOR events:", first)
    for goal in MOVED_FIELDS:
        assert [event["mode"] for event in first[goal]] == ["moved", "reasoning"], goal
        assert first[goal][0]["locator"] == before[goal]["locator"]
    for goal in UNMOVED:
        assert [event["mode"] for event in first[goal]] == ["memory"], goal
    learned = {goal: first[goal][1]["locator"] for goal in MOVED_FIELDS}
    for goal, locator in learned.items():
        assert remembered(db, goal)["locator"] == locator and remembered(db, goal)["hire"] == HIRE
    calls = len(atlas_runner.llm.prompts)
    run1_shot_count = len(atlas_runner.shots)

    reset_dev_hire(gitea, container)
    second_run = atlas_runner.run(headless=headless)
    second = {goal: locator_events(db, second_run, goal) for goal in MOVED_FIELDS + UNMOVED}
    print("run 2 LOCATOR events:", second)
    for goal in MOVED_FIELDS:
        assert [(event["mode"], event["locator"], event["source_hire"]) for event in second[goal]] == [
            ("memory", learned[goal], HIRE)
        ], goal
    assert len(atlas_runner.llm.prompts) == calls, "run 2 reasoned about a locator it had already learned"

    print(f"timings: run 1 (moved, reasoning) {atlas_runner.durations_s[0]:.1f}s, "
          f"run 2 (from memory) {atlas_runner.durations_s[1]:.1f}s, LLM calls {calls}")
    print("screenshots:\n" + "\n".join(atlas_runner.shots))
    assert all(path.endswith("-memory.png") for path in atlas_runner.shots[run1_shot_count:])
