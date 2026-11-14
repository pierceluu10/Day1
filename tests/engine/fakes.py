"""Fake parts for driving the real reconcile loop and the real Atlas store without Docker, Gitea or a browser.

Steps keep their effects on the host: run() writes a marker file and appends to a run log; check() reads the
marker by a different route (file existence) and also appends to the log, so tests can see order across processes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dayone.contracts import (
    CheckResult,
    ExecResult,
    Failure,
    FixProposal,
    Hire,
    MachineFacts,
    Resolution,
    RunResult,
    Step,
    StepContext,
    StepKind,
)
from dayone.engine.runner import OpenParts, Parts
from dayone.store.atlas import AtlasStore
from dayone.store.events import AtlasEventBus

HANG_AFTER_RUN_ENV = "FAKE_HANG_AFTER_RUN"  # step id whose run() blocks forever after its effect
HUNG_MARKER = "hung"
HANG_SLEEP_S = 3600


def log_line(workdir: Path, line: str) -> None:
    with (workdir / "run.log").open("a") as log:
        log.write(f"{line} {os.getpid()}\n")


def log_lines(workdir: Path) -> list[str]:
    path = workdir / "run.log"
    return path.read_text().splitlines() if path.exists() else []


def marker_step(workdir: Path, step_id: str) -> Step:
    marker = workdir / f"{step_id}.done"

    def run(context: StepContext) -> RunResult:
        log_line(workdir, f"run {step_id}")
        marker.write_text(context.idempotency_key)
        if os.environ.get(HANG_AFTER_RUN_ENV) == step_id:
            (workdir / HUNG_MARKER).touch()
            time.sleep(HANG_SLEEP_S)
        return RunResult(ok=True, output=f"wrote {marker.name}")

    def check(context: StepContext) -> CheckResult:
        log_line(workdir, f"check {step_id}")
        if marker.exists():
            return CheckResult(ok=True, evidence=f"{marker.name} present")
        return CheckResult(ok=False, evidence=f"{marker.name} missing", error_text=f"no such file: {marker.name}")

    return Step(id=step_id, kind=StepKind.TERMINAL, title=step_id, check=check, run=run)


class FakeExecutor:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str | None]] = []
        self.results: dict[str, tuple[int, str]] = {}  # command -> (exit code, output); default success, silent

    def ensure_container(self) -> None:
        pass

    def exec(self, command: str, timeout_s: float, workdir: str | None = None) -> ExecResult:
        self.commands.append((command, workdir))
        exit_code, output = self.results.get(command, (0, ""))
        return ExecResult(command=command, exit_code=exit_code, output=output, duration_s=0.0)

    def facts(self) -> MachineFacts:
        return MachineFacts(os_family="ubuntu", os_version="22.04", package_manager="apt")

    def remove_container(self) -> None:
        pass


class FakeBrowser:
    def __init__(self) -> None:
        self.ticked: list[str] = []

    def tick_checklist(self, item: str) -> None:
        self.ticked.append(item)

    def close(self) -> None:
        pass


@dataclass
class FakeGitea:
    head: str = "a" * 40
    head_reads: int = 0

    def branch_head(self, repo: str, branch: str) -> str:
        self.head_reads += 1
        return self.head

    def user(self, username: str) -> dict:
        return {"login": username}

    def user_keys(self, username: str) -> list[dict]:
        return []

    def welcome_issue(self, username: str) -> dict:
        return {}

    def find_issue(self, repo: str, title: str) -> dict | None:
        return None

    def create_issue(self, repo: str, title: str, body: str) -> dict:
        return {"title": title}


@dataclass
class FakeMemory:
    """Proposes each of `fixes` in turn (as exact memory) until one makes try_fix pass."""

    fixes: list[FixProposal] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    issue_urls: list[str] = field(default_factory=list)
    close_loop_error: Exception | None = None
    close_loop_calls: int = 0

    def resolve(self, failure: Failure, try_fix: Callable[[FixProposal], CheckResult]) -> Resolution:
        self.failures.append(failure)
        last = CheckResult(ok=False, evidence="no fix known", error_text=failure.error_text)
        for attempt, proposal in enumerate(self.fixes, start=1):
            last = try_fix(proposal)
            if last.ok:
                return Resolution(fixed=True, proposal=proposal, attempts=attempt, llm_calls=0, last_check=last)
        return Resolution(fixed=False, proposal=None, attempts=len(self.fixes), llm_calls=0, last_check=last)

    def find_locator(self, goal: str, page_summary: str) -> None:
        return None

    def save_locator(self, hint: object, hire: str) -> None:
        pass

    def close_loop(self) -> list[str]:
        self.close_loop_calls += 1
        if self.close_loop_error is not None:
            raise self.close_loop_error
        return self.issue_urls


def fake_fingerprint(error_text: str) -> str:
    return f"fp:{error_text}"


def parts_opener(
    store: AtlasStore,
    steps: list[Step],
    gitea: FakeGitea,
    memory: FakeMemory,
    browser: FakeBrowser,
    executor: FakeExecutor | None = None,
) -> OpenParts:
    @contextmanager
    def open_parts(hire: Hire) -> Iterator[Parts]:
        events = AtlasEventBus(store.db, hire)
        try:
            yield Parts(
                events=events,
                executor=executor or FakeExecutor(),
                browser=browser,
                gitea=gitea,
                memory=memory,
                steps=steps,
                fingerprint=fake_fingerprint,
                exec_timeout_s=1.0,
            )
        finally:
            events.close()

    return open_parts
