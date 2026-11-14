"""Locator memory recovers from a stale saved locator, end to end on the real stack.

Real Atlas (dev DB), real ExperienceMemory with Voyage embeddings, real AnthropicLLM, real AtlasEventBus.
A deliberately stale locator for the add-key button is saved as if hire `ada` had learned it; linus's
b_add_ssh_key must show red, reason (amber), save the new locator, and the next run must use it (green)
with no LLM call.
"""

from __future__ import annotations

import dataclasses
import shutil
import uuid

import pytest
from pymongo import MongoClient

from dayone.browser.locators import KEYS_OPEN_FORM
from dayone.browser.session import PROFILE_DIR, PlaywrightSession
from dayone.config import settings
from dayone.contracts import EventType, Hire, LocatorHint, Machine, StepContext, StepKind, idempotency_key
from dayone.llm.anthropic import AnthropicLLM
from dayone.llm.voyage import VoyageEmbedder
from dayone.memory.experience import EXPERIENCES, ExperienceMemory
from dayone.store.events import AtlasEventBus

from conftest import HIRE, DockerExec, RecordingEvents
from test_browser_steps import reconcile

STALE_LOCATOR = "#add-ssh-key-button-v1"  # what the keys page used to call its button
STALE_SOURCE = "ada"
KEYS_PAGE_SUMMARY = "SSH / GPG Keys - Gitea: Git with a cup of tea (/user/settings/keys)"
STALE_BBOX = (1277.0, 71.0, 69.0, 32.0)


class CountingLLM(AnthropicLLM):
    def __init__(self, config):
        super().__init__(config)
        self.calls = 0

    def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        self.calls += 1
        return super().complete(system, prompt, max_tokens)


@pytest.fixture(scope="module")
def db():
    client = MongoClient(settings().mongodb_uri, tz_aware=True)  # as AtlasStore does; AtlasEventBus compares aware ts
    assert settings().mongodb_db.startswith("dayone_dev_"), "this test writes locators; never run it on the demo DB"
    yield client[settings().mongodb_db]
    client.close()


@pytest.fixture(scope="module")
def memory(db, gitea):
    memory = ExperienceMemory(db, AnthropicLLM(settings()), VoyageEmbedder(settings()), RecordingEvents(), gitea, settings())
    memory.ensure_indexes()
    return memory


def run_add_ssh_key(make_ssh_context, run_id: str):
    """One hire run of sign-in + b_add_ssh_key with a fresh browser, as after a restart."""
    ctx = make_ssh_context(run_id)
    for step_id in ("b_sign_in", "b_add_ssh_key"):
        ctx.step_id, ctx.idempotency_key = step_id, idempotency_key(HIRE, step_id)
        _, after = reconcile(step_id, ctx)
        assert after.ok, after
    ctx.events.close()
    return ctx


def locator_events(db, run_id: str) -> list[dict]:
    query = {"hire": HIRE, "run_id": run_id, "type": EventType.LOCATOR.value, "data.goal": KEYS_OPEN_FORM}
    return [event["data"] for event in db["events"].find(query).sort([("ts", 1), ("seq", 1)])]


def test_stale_saved_locator_is_reasoned_saved_and_reused(clean_linus, db, memory, gitea, container, screenshots):
    db[EXPERIENCES].delete_many({"kind": StepKind.BROWSER, "goal": KEYS_OPEN_FORM})
    memory.save_locator(LocatorHint(KEYS_OPEN_FORM, STALE_LOCATOR, STALE_BBOX, KEYS_PAGE_SUMMARY, STALE_SOURCE, None), STALE_SOURCE)
    llm = CountingLLM(settings())
    shots: list[str] = []
    browsers: list[PlaywrightSession] = []

    def make_ssh_context(run_id: str) -> StepContext:
        hire = Hire(HIRE, Machine.UBUNTU, container, run_id)
        events = AtlasEventBus(db, hire)
        browser = PlaywrightSession(hire, dataclasses.replace(settings(), browser_headless=True), events, memory, llm)
        browsers.append(browser)
        nonlocal shots
        shots = screenshots(browser)  # the same list for every browser in this test
        return StepContext(hire, "b_sign_in", "", DockerExec(container), browser, gitea, events, memory)

    try:
        first_run = f"recovery-{uuid.uuid4().hex[:8]}"
        run_add_ssh_key(make_ssh_context, first_run)
        first = locator_events(db, first_run)
        print("run 1 events:", first)
        assert [event["mode"] for event in first] == ["moved", "reasoning"]
        assert first[0]["locator"] == STALE_LOCATOR and first[0]["source_hire"] == STALE_SOURCE
        learned = first[1]["locator"]
        reasoning_calls = llm.calls
        run1_shot_count = len(shots)
        assert reasoning_calls >= 1

        saved = db[EXPERIENCES].find_one({"kind": StepKind.BROWSER, "goal": KEYS_OPEN_FORM, "locator": learned})
        print("saved:", {k: v for k, v in saved.items() if k != "embedding"})
        assert saved["hire"] == HIRE and saved["outcome"] == "verified" and saved["bbox"]

        # A second run from scratch: no key, no browser profile. The add-key button must come from memory.
        for key in gitea.user_keys(HIRE):
            gitea.request("DELETE", f"/admin/users/{HIRE}/keys/{key['id']}")
        browsers[-1].close()
        shutil.rmtree(PROFILE_DIR / HIRE)
        second_run = f"recovery-{uuid.uuid4().hex[:8]}"
        run_add_ssh_key(make_ssh_context, second_run)
        second = locator_events(db, second_run)
        print("run 2 events:", second)
        assert [event["mode"] for event in second] == ["memory"]
        assert second[0]["locator"] == learned and second[0]["source_hire"] == HIRE
        assert llm.calls == reasoning_calls, "the second run called the LLM for a locator it had already learned"
        print("screenshots:", "\n".join(shots))
        run1_shots, run2_shots = shots[:run1_shot_count], shots[run1_shot_count:]
        assert [path.rsplit("-", 1)[1] for path in run1_shots].count("moved.png") == 1
        assert [path.rsplit("-", 1)[1] for path in run1_shots].count("reasoning.png") == 1
        assert all(path.endswith("-memory.png") for path in run2_shots)
    finally:
        for browser in browsers:
            browser.close()
