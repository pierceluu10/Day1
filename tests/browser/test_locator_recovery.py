"""Locator memory recovers from a stale saved locator, end to end on the real stack.

A deliberately stale locator for the add-key button is saved as if hire `ada` had learned it; the dev hire's
b_add_ssh_key must show red, reason (amber), save the new locator, and the next run must use it (green)
with no LLM call.
"""

from __future__ import annotations

from dayone.browser.locators import KEYS_OPEN_FORM, SEED_SOURCE
from dayone.contracts import LocatorHint, StepKind
from dayone.memory.experience import EXPERIENCES

from conftest import HIRE, locator_events, reset_dev_hire

STALE_LOCATOR = "#add-ssh-key-button-v1"  # what the keys page used to call its button
# The add-key button loses the id the hand-found seed relies on, so the stale memory and the seed both fail
# and only reasoning can find it. (scripts/move_ui.sh moves the form fields instead: test_moved_ui.py.)
RENAME_BUTTON_JS = """document.addEventListener('DOMContentLoaded', () => {
    const button = document.querySelector('#add-ssh-button');
    if (button) button.id = 'ssh-add-key-v2';
})"""
STALE_SOURCE = "ada"
KEYS_PAGE_SUMMARY = "SSH / GPG Keys - Gitea: Git with a cup of tea (/user/settings/keys)"
STALE_BBOX = (1277.0, 71.0, 69.0, 32.0)


def test_stale_saved_locator_is_reasoned_saved_and_reused(clean_hire, db, atlas_memory, atlas_runner, gitea, container):
    db[EXPERIENCES].delete_many({"kind": StepKind.BROWSER, "goal": KEYS_OPEN_FORM})
    atlas_memory.save_locator(
        LocatorHint(KEYS_OPEN_FORM, STALE_LOCATOR, STALE_BBOX, KEYS_PAGE_SUMMARY, STALE_SOURCE, None), STALE_SOURCE
    )

    first = locator_events(db, atlas_runner.run(init_script=RENAME_BUTTON_JS), KEYS_OPEN_FORM)
    print("run 1 events:", first)
    assert [event["mode"] for event in first] == ["moved", "moved", "reasoning"]
    assert first[0]["locator"] == STALE_LOCATOR and first[0]["source_hire"] == STALE_SOURCE
    assert first[1]["source_hire"] == SEED_SOURCE
    learned = first[2]["locator"]
    reasoning_calls = len(atlas_runner.llm.prompts)
    run1_shots = list(atlas_runner.shots)
    assert reasoning_calls >= 1

    saved = db[EXPERIENCES].find_one({"kind": StepKind.BROWSER, "goal": KEYS_OPEN_FORM, "locator": learned})
    print("saved:", {k: v for k, v in saved.items() if k != "embedding"})
    assert saved["hire"] == HIRE and saved["outcome"] == "verified" and saved["bbox"]

    # A second run from scratch (no key, no browser profile): the add-key button must come from memory.
    reset_dev_hire(gitea, container)
    second = locator_events(db, atlas_runner.run(init_script=RENAME_BUTTON_JS), KEYS_OPEN_FORM)
    print("run 2 events:", second)
    assert [event["mode"] for event in second] == ["memory"]
    assert second[0]["locator"] == learned and second[0]["source_hire"] == HIRE
    assert len(atlas_runner.llm.prompts) == reasoning_calls, "run 2 called the LLM for a locator it had learned"
    run2_shots = atlas_runner.shots[len(run1_shots):]
    print("screenshots:", "\n".join(atlas_runner.shots))
    assert [path.rsplit("-", 1)[1] for path in run1_shots].count("moved.png") == 2
    assert [path.rsplit("-", 1)[1] for path in run1_shots].count("reasoning.png") == 1
    assert all(path.endswith("-memory.png") for path in run2_shots)
