"""Shared interfaces between Day One's parts.

Owned by the orchestrator session. Do not change this file without telling it:
every other session builds against these names and shapes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


# --- Machines -----------------------------------------------------------------


class Machine(StrEnum):
    UBUNTU = "ubuntu"
    DEBIAN = "debian"
    ALPINE = "alpine"


MACHINE_IMAGES: dict[Machine, str] = {
    Machine.UBUNTU: "ubuntu:22.04",
    Machine.DEBIAN: "debian:12",
    Machine.ALPINE: "alpine:3.20",
}
HELD_OUT_MACHINE = Machine.ALPINE

DOCKER_NETWORK = "dayone-net"
GITEA_HOSTNAME = "gitea"  # name of the Gitea container on DOCKER_NETWORK
REPO = "acme/acme-api"
REPO_BRANCH = "main"
REPO_DIR = "/root/acme-api"  # clone location inside the hire's container
HIRES = ("ada", "grace", "linus", "barbara", "ken")


def container_name(hire: str) -> str:
    return f"dayone-{hire}"


@dataclass(frozen=True)
class MachineFacts:
    os_family: str  # "ubuntu" | "debian" | "alpine"
    os_version: str  # e.g. "22.04"
    package_manager: str  # "apt" | "apk"
    tools: dict[str, str] = field(default_factory=dict)  # tool -> version string

    def summary(self) -> str:
        tools = ", ".join(f"{name} {version}" for name, version in sorted(self.tools.items()))
        return f"{self.os_family} {self.os_version}, {self.package_manager}" + (f", {tools}" if tools else "")


# --- Steps ----------------------------------------------------------------------


class StepKind(StrEnum):
    TERMINAL = "terminal"
    BROWSER = "browser"


class StepStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"  # intent written; run() may have started; check before any re-run
    VERIFIED = "verified"  # run() then check() passed on the first try
    SKIPPED = "skipped"  # check() passed before run(): reconciled, nothing executed
    FIXED_FROM_MEMORY = "fixed_from_memory"
    FIXED_BY_REASONING = "fixed_by_reasoning"
    STALE = "stale"  # was done, but drift (remote HEAD moved / a re-check failed) invalidated it
    FAILED = "failed"


DONE_STATUSES = frozenset(
    {StepStatus.VERIFIED, StepStatus.SKIPPED, StepStatus.FIXED_FROM_MEMORY, StepStatus.FIXED_BY_REASONING}
)


@dataclass(frozen=True)
class ExecResult:
    command: str
    exit_code: int
    output: str  # stdout and stderr interleaved
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    evidence: str  # one human line of real output, e.g. "local HEAD 3f2a1c9 = API HEAD 3f2a1c9"
    detail: dict[str, Any] = field(default_factory=dict)  # raw values: versions, HTTP status, counts
    error_text: str = ""  # on failure: the real error output memory fingerprints and embeds


@dataclass(frozen=True)
class RunResult:
    ok: bool
    output: str
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hire:
    name: str
    machine: Machine
    container: str
    run_id: str  # new per `dayone start`, kept across `dayone resume`


@dataclass
class StepContext:
    hire: Hire
    step_id: str
    idempotency_key: str  # idempotency_key(hire, step_id); use it for any non-repeatable write
    executor: Executor
    browser: BrowserSession
    gitea: Gitea
    events: EventBus
    memory: Memory


@dataclass(frozen=True)
class Step:
    id: str
    kind: StepKind
    title: str
    check: Callable[[StepContext], CheckResult]  # must use a different route from run()
    run: Callable[[StepContext], RunResult]  # the naive action the README/checklist implies


def idempotency_key(hire: str, step_id: str) -> str:
    return f"dayone:{hire}:{step_id}"


# Ordered. Engine runs them in this order; ids are stable keys in Atlas.
STEP_IDS: tuple[str, ...] = (
    "t_base_tools",  # install git, openssh client, curl, ca-certs; check: versions via `--version`
    "t_ssh_keygen",  # ed25519 key, comment = idempotency key; check: `ssh-keygen -lf` fingerprint
    "b_sign_in",  # browser login as hire; check: API sees the session (e.g. /user via cookie) independent of the form
    "b_add_ssh_key",  # browser settings form, title carries idempotency key; check: API lists fingerprint AND `ssh -T` ok
    "b_open_welcome",  # browser opens the hire's Welcome issue; check: page issue number/title match the API
    "t_clone",  # git clone over SSH; check: local HEAD == API branch HEAD (this is also the drift probe)
    "t_runtime",  # venv with the required Python; check: venv python satisfies requires-python
    "t_system_package",  # naive: install what README lists (nothing); check: native deps build (pip wheel)
    "t_install_deps",  # pip install -r requirements.txt; check: `pip check` + import acme_api
    "t_settings",  # naive: nothing; check: app settings load
    "t_init_db",  # naive: nothing; check: db schema present
    "t_run_tests",  # pytest; check: exit 0 and passed == collected, evidence "14 passed"
    "b_checklist",  # browser ticks remaining Welcome items; check: API issue body has every item ticked
)


# Task-list items in each hire's Welcome issue (seeded by infra, ticked by the browser, in this order).
WELCOME_ISSUE_TITLE = "Welcome to Acme, {hire}!"
WELCOME_CHECKLIST: dict[str, str] = {
    "b_add_ssh_key": "Add your laptop's SSH key to your Gitea account",
    "t_clone": "Clone acme/acme-api",
    "t_install_deps": "Install the project dependencies",
    "t_settings": "Configure your local settings",
    "t_init_db": "Initialise the local database",
    "t_run_tests": "Run the test suite and see it pass",
}

# --- Memory -------------------------------------------------------------------


class FixSource(StrEnum):
    EXACT_MEMORY = "exact_memory"  # fingerprint match; zero LLM calls
    SEMANTIC_MEMORY = "semantic_memory"  # $vectorSearch hit, possibly adapted (apt -> apk)
    REASONING = "reasoning"  # LLM, with nearest experiences as hints


@dataclass(frozen=True)
class Failure:
    hire: str
    step_id: str
    kind: StepKind
    error_text: str
    fingerprint: str
    facts: MachineFacts

    def context_summary(self) -> str:
        return f"step {self.step_id} on {self.facts.summary()}"


@dataclass(frozen=True)
class FixProposal:
    commands: tuple[str, ...]  # shell commands run in the hire's container, in REPO_DIR when it exists
    source: FixSource
    rationale: str
    experience_id: str | None = None
    similarity: float | None = None  # vectorSearchScore, shown on screen
    source_hire: str | None = None  # whose experience this came from
    adapted_from: str | None = None  # e.g. "apt" when commands were adapted to apk


@dataclass(frozen=True)
class Resolution:
    fixed: bool
    proposal: FixProposal | None  # the fix that made the check pass
    attempts: int
    llm_calls: int
    last_check: CheckResult


@dataclass(frozen=True)
class LocatorHint:
    goal: str  # e.g. "ssh key form: title field"
    locator: str  # Playwright locator string that worked
    bbox: tuple[float, float, float, float] | None
    page_summary: str
    source_hire: str | None
    similarity: float | None


# --- Ledger records -----------------------------------------------------------


@dataclass(frozen=True)
class StepRecord:
    hire: str
    step_id: str
    status: StepStatus
    run_count: int  # incremented when intent is written; acceptance test 2 asserts it never exceeds need
    check_ever_failed: bool  # false at verification => flagged untrusted
    evidence: str
    updated_at: datetime
    fix_source: FixSource | None = None
    experience_id: str | None = None


# --- Interfaces -----------------------------------------------------------------


class Store(Protocol):
    """Atlas-backed ledger. Collections: hires, steps (unique hire+step_id), events, experiences."""

    def acquire_lease(self, hire: str, owner: str, ttl_s: float) -> bool: ...
    def renew_lease(self, hire: str, owner: str, ttl_s: float) -> bool: ...
    def release_lease(self, hire: str, owner: str) -> None: ...
    def upsert_hire(self, hire: Hire) -> None: ...
    def get_hire(self, name: str) -> Hire | None: ...
    def finish_hire(self, hire: str, ok: bool) -> None: ...
    def steps(self, hire: str) -> dict[str, StepRecord]: ...
    def record_check(self, hire: str, step_id: str, result: CheckResult) -> None: ...
    def mark_pending(self, hire: str, step_id: str, idempotency_key: str) -> StepRecord: ...
    def mark_done(
        self, hire: str, step_id: str, status: StepStatus, check: CheckResult, resolution: Resolution | None
    ) -> StepRecord: ...
    def mark_stale(self, hire: str, step_ids: list[str], reason: str) -> None: ...
    def mark_failed(self, hire: str, step_id: str, check: CheckResult) -> None: ...
    def reset_hire(self, hire: str) -> None: ...


class Executor(Protocol):
    """Runs commands in one hire's container via `docker exec` (sh -lc on every OS)."""

    def ensure_container(self) -> None: ...
    def exec(self, command: str, timeout_s: float, workdir: str | None = None) -> ExecResult: ...
    def facts(self) -> MachineFacts: ...
    def remove_container(self) -> None: ...


class BrowserSession(Protocol):
    """One headed Chromium per run, overlay injected. Browser steps get the page through the concrete class."""

    def close(self) -> None: ...


class Gitea(Protocol):
    """Read-back API used by checks (admin token). Never used to perform the browser steps themselves."""

    def user(self, username: str) -> dict[str, Any]: ...
    def user_keys(self, username: str) -> list[dict[str, Any]]: ...
    def branch_head(self, repo: str, branch: str) -> str: ...
    def welcome_issue(self, username: str) -> dict[str, Any]: ...
    def find_issue(self, repo: str, title: str) -> dict[str, Any] | None: ...
    def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]: ...


class Memory(Protocol):
    def resolve(self, failure: Failure, try_fix: Callable[[FixProposal], CheckResult]) -> Resolution:
        """Exact fingerprint -> $vectorSearch (adapting across package managers) -> LLM (max 3 attempts).
        try_fix runs the proposal's commands and returns the step's check. Saves the experience on success."""
        ...

    def find_locator(self, goal: str, page_summary: str) -> LocatorHint | None: ...
    def save_locator(self, hint: LocatorHint, hire: str) -> None: ...
    def close_loop(self) -> list[str]:
        """Open one README-change issue per fix applied for 2+ hires (idempotent by title). Returns issue URLs."""
        ...


class LLM(Protocol):
    def complete(self, system: str, prompt: str, max_tokens: int) -> str: ...


class Embedder(Protocol):
    def embed(self, texts: list[str], input_type: str) -> list[list[float]]: ...  # input_type: "document" | "query"


# --- Events ---------------------------------------------------------------------


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    RUN_RESUMED = "run_resumed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    STEP_CHECKING = "step_checking"
    STEP_CHECK_RESULT = "step_check_result"
    STEP_PENDING = "step_pending"  # intent written, run() about to start
    STEP_STATUS = "step_status"  # final status for this pass, with evidence
    TERMINAL_OUTPUT = "terminal_output"  # data: {"command", "chunk"}
    ERROR_CAPTURED = "error_captured"  # data: {"fingerprint", "error_text"}
    MEMORY_HIT = "memory_hit"  # data: FixProposal fields
    MEMORY_MISS = "memory_miss"
    REASONING_STARTED = "reasoning_started"
    FIX_ATTEMPT = "fix_attempt"  # data: {"source", "commands", "ok"}
    EXPERIENCE_SAVED = "experience_saved"
    DRIFT_DETECTED = "drift_detected"  # data: {"from", "to"} or {"step_id", "reason"}
    LOCATOR = "locator"  # data: {"goal", "mode": "memory"|"reasoning"|"moved", "locator"}
    README_ISSUE_OPENED = "readme_issue_opened"


@dataclass(frozen=True)
class Event:
    hire: str
    run_id: str
    type: EventType
    ts: datetime
    step_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class EventBus(Protocol):
    """Events are written to Atlas `events`; the API tails them, so the engine can run in another process."""

    def emit(self, type: EventType, step_id: str | None = None, **data: Any) -> None: ...
    def tail(self, hire: str, after: datetime | None) -> Iterator[Event]: ...
