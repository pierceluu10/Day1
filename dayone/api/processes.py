"""Engine processes: the CLI records its pid per hire, the API spawns detached runs and reports liveness.

The engine always runs in its own session (process group), so `kill -9` on it never reaches the page.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dayone.config import ROOT, STATE_DIR

PID_DIR = STATE_DIR / "runs"
LOG_DIR = STATE_DIR / "logs"

# Children this process spawned; polling them reaps a killed run so it never lingers as a live zombie.
_children: dict[int, subprocess.Popen] = {}


def pid_file(hire: str) -> Path:
    return PID_DIR / f"{hire}.pid"


def log_file(hire: str) -> Path:
    return LOG_DIR / f"{hire}.log"


def record_pid(hire: str) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    pid_file(hire).write_text(str(os.getpid()))


def clear_pid(hire: str) -> None:
    pid_file(hire).unlink(missing_ok=True)


def _reap_children() -> None:
    """Waits on every exited child, including runs that cleared their own pid file and so are never looked up."""
    for pid, child in list(_children.items()):
        if child.poll() is not None:
            del _children[pid]


def running_pid(hire: str) -> int | None:
    """Pid of the live engine for this hire, or None. A `kill -9` leaves the pid file behind; liveness decides.
    shortcut: a reused pid after a reboot reads as running, until the pid file is cleared by the next run."""
    _reap_children()
    try:
        pid = int(pid_file(hire).read_text())
    except (FileNotFoundError, ValueError):
        return None
    if pid in _children:
        return pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def spawn(hire: str, cli_args: list[str]) -> int:
    """Run `dayone <cli_args>` detached, output appended to the hire's log. Returns the pid."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with log_file(hire).open("ab") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "dayone.cli", *cli_args],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _children[child.pid] = child
    return child.pid
