"""Engine liveness as the page sees it: a `kill -9` must read as stopped, never as a live zombie."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import types

import pytest

from dayone import cli
from dayone.api import processes
from dayone.contracts import Machine

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


def test_spawned_engine_that_exits_cleanly_is_reaped_even_after_clearing_its_pid_file(monkeypatch):
    # The CLI clears its pid file on the way out, so the API never looks that hire up again.
    real_popen = subprocess.Popen
    monkeypatch.setattr(processes.subprocess, "Popen", lambda args, **kwargs: real_popen([sys.executable, "-c", "pass"], **kwargs))

    pid = processes.spawn("grace", ["start", "--hire", "grace", "--machine", "debian"])
    deadline = time.monotonic() + REAP_TIMEOUT_S
    while pid in processes._children and time.monotonic() < deadline:
        processes.running_pid("ada")  # any liveness check, as the page's polling does
        time.sleep(0.05)

    assert pid not in processes._children
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # already reaped: no zombie left behind


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


@pytest.fixture
def engine(monkeypatch):
    """Stands in for `dayone.engine.runner` (session 2) and records how the CLI calls it."""
    calls = []
    runner = types.ModuleType("dayone.engine.runner")
    runner.outcome = True
    for name, base in [("LeaseHeld", RuntimeError), ("HireNotFound", LookupError), ("MachineMismatch", ValueError), ("MachineRequired", ValueError)]:
        setattr(runner, name, type(name, (base,), {}))

    def run_hire(name, machine, resume):
        calls.append(("run_hire", name, machine, resume))
        assert processes.running_pid(name) == os.getpid(), "the engine must be visible as running while it works"
        if isinstance(runner.outcome, Exception):
            raise runner.outcome
        return runner.outcome

    runner.run_hire = run_hire
    runner.reset = lambda name: calls.append(("reset", name))
    monkeypatch.setitem(sys.modules, "dayone.engine.runner", runner)
    runner.calls = calls
    return runner


@pytest.mark.parametrize(("outcome", "code"), [(True, cli.EXIT_OK), (False, cli.EXIT_RUN_FAILED)])
def test_cli_start_maps_the_engine_result_to_the_exit_code(engine, outcome, code):
    engine.outcome = outcome

    assert cli.main(["start", "--hire", "ken", "--machine", "alpine"]) == code
    assert engine.calls == [("run_hire", "ken", Machine.ALPINE, False)]
    assert processes.running_pid("ken") is None


@pytest.mark.parametrize(
    ("error", "code"),
    [("LeaseHeld", cli.EXIT_ALREADY_RUNNING), ("HireNotFound", cli.EXIT_NOTHING_TO_RESUME), ("MachineMismatch", cli.EXIT_WRONG_MACHINE)],
)
def test_cli_maps_engine_refusals_to_exit_codes_and_clears_the_pid(engine, error, code):
    engine.outcome = getattr(engine, error)("refused")

    assert cli.main(["resume", "--hire", "ken"]) == code
    assert not processes.pid_file("ken").exists()


def test_cli_lets_unexpected_engine_errors_crash_but_still_clears_the_pid(engine):
    engine.outcome = KeyError("bug")

    with pytest.raises(KeyError):
        cli.main(["resume", "--hire", "ken"])
    assert not processes.pid_file("ken").exists()


@pytest.mark.parametrize(("argv", "hire"), [(["reset", "ada"], "ada"), (["reset", "--hire", "ada"], "ada"), (["reset"], None)])
def test_cli_reset_accepts_positional_and_option(engine, argv, hire):
    assert cli.main(argv) == cli.EXIT_OK
    assert engine.calls == [("reset", hire)]
