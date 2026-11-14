"""Engine liveness as the page sees it: a `kill -9` must read as stopped, never as a live zombie."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from dayone import cli
from dayone.api import processes

REAP_TIMEOUT_S = 5.0


@pytest.fixture(autouse=True)
def state_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "PID_DIR", tmp_path / "runs")
    monkeypatch.setattr(processes, "LOG_DIR", tmp_path / "logs")


def wait_until_stopped(hire: str) -> bool:
    deadline = time.monotonic() + REAP_TIMEOUT_S
    while time.monotonic() < deadline:
        if processes.running_pid(hire) is None:
            return True
        time.sleep(0.05)
    return False


def test_live_pid_is_running_and_cleared_pid_is_not():
    processes.record_pid("ada")
    assert processes.running_pid("ada") == os.getpid()
    processes.clear_pid("ada")
    assert processes.running_pid("ada") is None


def test_stale_pid_file_after_kill_reads_as_stopped():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    processes.PID_DIR.mkdir(parents=True)
    processes.pid_file("ada").write_text(str(child.pid))
    assert processes.running_pid("ada") == child.pid

    child.send_signal(signal.SIGKILL)
    child.wait()

    assert processes.running_pid("ada") is None


def test_spawned_engine_killed_with_sigkill_is_reaped_and_reads_as_stopped(monkeypatch):
    # Spawn through the real path, but make the child the sleeper instead of the engine.
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    real_popen = subprocess.Popen
    monkeypatch.setattr(processes.subprocess, "Popen", lambda args, **kwargs: real_popen(sleeper, **kwargs))

    pid = processes.spawn("ken", ["start", "--hire", "ken", "--machine", "alpine"])
    processes.PID_DIR.mkdir(parents=True)
    processes.pid_file("ken").write_text(str(pid))
    assert processes.running_pid("ken") == pid
    assert os.getpgid(pid) == pid, "the engine must lead its own process group so killing it spares the page"

    os.kill(pid, signal.SIGKILL)

    assert wait_until_stopped("ken"), "a SIGKILLed child of the API stayed 'running' (unreaped zombie)"


def test_cli_rejects_unknown_hire_with_usage_exit():
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["start", "--hire", "mallory", "--machine", "ubuntu"])
    assert exit_info.value.code == 2


def test_cli_resume_without_unfinished_hire(monkeypatch):
    monkeypatch.setattr(cli, "_unfinished_hire", lambda: None)
    assert cli.main(["resume"]) == cli.EXIT_NOTHING_TO_RESUME


def test_cli_refuses_a_second_engine_for_the_same_hire():
    processes.record_pid("barbara")  # this test process plays the running engine
    assert cli.main(["resume", "--hire", "barbara"]) == cli.EXIT_ALREADY_RUNNING
