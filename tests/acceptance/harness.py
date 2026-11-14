"""Drives the real `dayone` CLI as subprocesses and watches its events live in Atlas."""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pymongo.database import Database

from atlas import EVENT_ORDER
from dayone.config import ROOT
from dayone.contracts import EventType, container_name

DAYONE = Path(sys.executable).parent / "dayone"
RUN_TIMEOUT_S = 20 * 60.0
CLI_TIMEOUT_S = 180.0
EVENT_POLL_S = 0.2
OUTPUT_TAIL_CHARS = 6000
STEP_EVENTS = frozenset({EventType.STEP_CHECKING.value, EventType.STEP_PENDING.value})

SYSTEM_MODULES = (
    "dayone.cli",
    "dayone.engine.runner",
    "dayone.store.atlas",
    "dayone.store.events",
    "dayone.gitea",
    "dayone.terminal.docker",
    "dayone.terminal.fingerprint",
    "dayone.browser.session",
    "dayone.memory.experience",
    "dayone.llm.anthropic",
    "dayone.llm.voyage",
    "dayone.steps",
    "dayone.steps.terminal_steps",
    "dayone.steps.browser_steps",
)


def missing_modules(*modules: str) -> list[str]:
    """Modules of ours that do not exist yet. An import error inside an existing module is raised, not skipped."""
    missing = []
    for module in modules:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as error:
            if not error.name or not (module == error.name or module.startswith(error.name + ".")):
                raise
            missing.append(module)
    return missing


def requires(*modules: str) -> pytest.MarkDecorator:
    missing = missing_modules(*modules)
    return pytest.mark.skipif(bool(missing), reason=f"needs {', '.join(missing)} (not importable yet)")


requires_system = requires(*SYSTEM_MODULES)


def distinct(hires: dict[str, str], *roles: str) -> None:
    """Skip a test whose roles must be different people (comparing a run with itself proves nothing)."""
    names = [hires[role] for role in roles]
    if len(set(names)) < len(names):
        pytest.skip(f"needs distinct hires for roles {roles}, got {names}")


class CliFailed(AssertionError):
    pass


class AgentTimeout(AssertionError):
    pass


class AgentExitedEarly(AssertionError):
    pass


def tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


def run_cli(*args: str, timeout_s: float = CLI_TIMEOUT_S) -> str:
    print(f"$ dayone {' '.join(args)}")
    result = subprocess.run(
        [str(DAYONE), *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout_s, env=os.environ.copy()
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise CliFailed(f"dayone {' '.join(args)} exited {result.returncode}:\n{tail(output)}")
    return output


def container_exec(hire: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container_name(hire), "sh", "-lc", command],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
    )


def container_exists(hire: str) -> bool:
    return subprocess.run(["docker", "inspect", container_name(hire)], capture_output=True).returncode == 0


class Agent:
    """One `dayone` process in its own process group, so kill() takes its Chromium and docker exec clients with it."""

    def __init__(self, log_dir: Path, *args: str) -> None:
        self.args = args
        self.log = log_dir / f"{'_'.join(args).replace('-', '')}_{time.time_ns()}.log"
        self._log_file = self.log.open("w")
        self.process = subprocess.Popen(
            [str(DAYONE), *args],
            cwd=ROOT,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
        print(f"started `dayone {' '.join(args)}` pid {self.process.pid}, log {self.log}")

    def wait(self, timeout_s: float = RUN_TIMEOUT_S) -> int:
        try:
            code = self.process.wait(timeout_s)
        except subprocess.TimeoutExpired as error:
            self.kill()
            raise AgentTimeout(f"`dayone {' '.join(self.args)}` ran past {timeout_s}s:\n{self.output_tail()}") from error
        self._log_file.close()
        print(f"`dayone {' '.join(self.args)}` exited {code}")
        return code

    def kill(self) -> None:
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()
        self._log_file.close()
        print(f"kill -9 `dayone {' '.join(self.args)}` pid {self.process.pid}")

    def output_tail(self) -> str:
        self._log_file.flush()
        return tail(self.log.read_text())


class EventWatcher:
    """Tails one hire's new events in order and tracks the current step (terminal output may carry no step id)."""

    def __init__(self, db: Database, hire: str) -> None:
        self.db = db
        self.hire = hire
        existing = list(db.events.find({"hire": hire}, {"_id": 1, "ts": 1}))
        self.seen = {event["_id"] for event in existing}
        self.since = max((event["ts"] for event in existing), default=None)
        self.current_step: str | None = None

    def poll(self) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"hire": self.hire}
        if self.since is not None:
            query["ts"] = {"$gte": self.since}
        fresh = [event for event in self.db.events.find(query).sort(EVENT_ORDER) if event["_id"] not in self.seen]
        for event in fresh:
            self.seen.add(event["_id"])
            self.since = event["ts"]
        return fresh

    def wait_for(
        self,
        agent: Agent,
        description: str,
        predicate: Callable[[dict[str, Any], str | None], bool],
        timeout_s: float = RUN_TIMEOUT_S,
    ) -> dict[str, Any]:
        """First new event for which predicate(event, current_step) holds; fails if the agent exits first."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for event in self.poll():
                if event["type"] in STEP_EVENTS and event.get("step_id"):
                    self.current_step = event["step_id"]
                if predicate(event, self.current_step):
                    print(f"seen {description}: {event['type']} step={event.get('step_id')} current={self.current_step}")
                    return event
            if agent.process.poll() is not None:
                raise AgentExitedEarly(
                    f"`dayone {' '.join(agent.args)}` exited {agent.process.returncode} before {description}:\n"
                    f"{agent.output_tail()}"
                )
            time.sleep(EVENT_POLL_S)
        agent.kill()
        raise AgentTimeout(f"no {description} within {timeout_s}s:\n{agent.output_tail()}")
