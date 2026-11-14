"""A green run proposes README changes for repeated fixes; that side effect can never fail the run."""

from __future__ import annotations

from pathlib import Path

from dayone.contracts import Machine
from dayone.engine.runner import drive
from dayone.gitea import GiteaUnreachable
from dayone.store.atlas import AtlasStore
from tests.engine.fakes import FakeBrowser, FakeExecutor, FakeGitea, FakeMemory, marker_step, parts_opener
from tests.engine.test_repair import needs_fix_step

ISSUE_URL = "http://localhost:3000/acme/acme-api/issues/7"


def _last_event(store: AtlasStore, hire: str) -> dict:
    return store.db["events"].find_one({"hire": hire}, sort=[("ts", -1), ("seq", -1)])


def test_green_run_calls_close_loop_once(store: AtlasStore, tmp_path: Path) -> None:
    memory = FakeMemory(issue_urls=[ISSUE_URL])
    opener = parts_opener(store, [marker_step(tmp_path, "s_one")], FakeGitea(), memory, FakeBrowser())
    assert drive(store, "green", Machine.UBUNTU, False, opener)
    assert memory.close_loop_calls == 1
    finished = _last_event(store, "green")
    assert finished["type"] == "run_finished" and finished["data"] == {"readme_issues": [ISSUE_URL]}


def test_failed_run_does_not_call_close_loop(store: AtlasStore, tmp_path: Path) -> None:
    executor, memory = FakeExecutor(), FakeMemory()
    opener = parts_opener(store, [needs_fix_step(tmp_path, executor)], FakeGitea(), memory, FakeBrowser(), executor)
    assert not drive(store, "red", Machine.UBUNTU, False, opener)
    assert memory.close_loop_calls == 0
    assert _last_event(store, "red")["type"] == "run_failed"


def test_gitea_failure_in_close_loop_keeps_the_run_green(store: AtlasStore, tmp_path: Path) -> None:
    memory = FakeMemory(close_loop_error=GiteaUnreachable("connection refused"))
    opener = parts_opener(store, [marker_step(tmp_path, "s_one")], FakeGitea(), memory, FakeBrowser())
    assert drive(store, "flaky_gitea", Machine.UBUNTU, False, opener)
    finished = _last_event(store, "flaky_gitea")
    print("run_finished data:", finished["data"])
    assert finished["type"] == "run_finished"
    assert finished["data"] == {"readme_issues": [], "close_loop_error": "GiteaUnreachable: connection refused"}
    assert store.db["hires"].find_one({"_id": "flaky_gitea"})["status"] == "finished"
