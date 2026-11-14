"""Acceptance test 2 in miniature: kill -9 between a step's run() and its check(), then resume."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dayone.config import ROOT, Settings
from dayone.contracts import StepStatus
from dayone.store.atlas import AtlasStore
from tests.engine.fake_hire import STEP_IDS
from tests.engine.fakes import HANG_AFTER_RUN_ENV, HUNG_MARKER, log_lines

HANG_WAIT_S = 30
RUN_TIMEOUT_S = 60
COMPARED_FIELDS = ("status", "run_count", "check_ever_failed", "untrusted", "evidence", "fix_source")


def _engine(test_settings: Settings, hire: str, workdir: Path, mode: str, hang_step: str | None = None):
    env = {**os.environ, "MONGODB_DB": test_settings.mongodb_db}
    env.pop(HANG_AFTER_RUN_ENV, None)
    if hang_step:
        env[HANG_AFTER_RUN_ENV] = hang_step
    return subprocess.Popen(
        [sys.executable, "-m", "tests.engine.fake_hire", hire, str(workdir), mode], cwd=ROOT, env=env
    )


def _wait_for(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + HANG_WAIT_S
    while not path.exists():
        assert process.poll() is None, f"engine exited early with {process.returncode}"
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.1)


def _ledger(store: AtlasStore, hire: str) -> dict[str, dict]:
    docs = store.db["steps"].find({"hire": hire})
    return {doc["step_id"]: {name: doc.get(name) for name in COMPARED_FIELDS} for doc in docs}


def test_kill_between_run_and_check_resumes_without_repeating(
    store: AtlasStore, test_settings: Settings, tmp_path: Path
) -> None:
    killed_dir, clean_dir = tmp_path / "killed", tmp_path / "clean"
    killed_dir.mkdir()
    clean_dir.mkdir()

    # Uninterrupted reference run.
    clean = _engine(test_settings, "clean", clean_dir, "start")
    assert clean.wait(RUN_TIMEOUT_S) == 0

    # Same task, killed after s_two's effect landed but before its check ran.
    victim = _engine(test_settings, "killed", killed_dir, "start", hang_step="s_two")
    _wait_for(killed_dir / HUNG_MARKER, victim)
    os.kill(victim.pid, signal.SIGKILL)
    victim.wait()
    before_resume = log_lines(killed_dir)
    print("log at kill:", before_resume)
    assert store.steps("killed")["s_two"].status == StepStatus.PENDING
    assert store.steps("killed")["s_two"].run_count == 1

    resumed = _engine(test_settings, "killed", killed_dir, "resume")
    assert resumed.wait(RUN_TIMEOUT_S) == 0

    log = log_lines(killed_dir)
    after = [line.rsplit(" ", 1)[0] for line in log[len(before_resume) :]]
    print("log after resume:", after)
    runs = [line.rsplit(" ", 1)[0] for line in log if line.startswith("run ")]
    assert sorted(runs) == sorted(f"run {step_id}" for step_id in STEP_IDS), "a run() executed twice"
    assert "run s_two" not in after, "pending step was re-run instead of checked"
    assert after[0] == "check s_one" and after[1] == "check s_two", "resume did not start by checking"

    killed, reference = _ledger(store, "killed"), _ledger(store, "clean")
    print("killed:", killed, "\nclean:", reference)
    assert killed == reference
    assert all(record["status"] == StepStatus.VERIFIED for record in reference.values())

    run_ids = store.db["events"].distinct("run_id", {"hire": "killed"})
    assert len(run_ids) == 1, "resume must keep the run_id"
    types = [doc["type"] for doc in store.db["events"].find({"hire": "killed"}).sort([("ts", 1), ("seq", 1)])]
    assert "run_resumed" in types and types[-1] == "run_finished"
