"""The reconcile loop: check first, write intent before effect, verify independently, repair from memory.

Every step is checked before it is run, so resuming after a crash is just running the loop again: done steps
pass their check and are not re-run, and a step left `pending` is checked before anything is executed.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from types import TracebackType

from playwright.sync_api import Error as PlaywrightError
from pymongo.errors import PyMongoError

from dayone.config import settings
from dayone.contracts import (
    DONE_STATUSES,
    REPO,
    REPO_BRANCH,
    REPO_DIR,
    WELCOME_CHECKLIST,
    BrowserSession,
    CheckResult,
    EventBus,
    EventType,
    Executor,
    Failure,
    FixProposal,
    FixSource,
    Gitea,
    Hire,
    Machine,
    Memory,
    Resolution,
    RunResult,
    Step,
    StepContext,
    StepStatus,
    Store,
    container_name,
    idempotency_key,
)
from dayone.memory.experience import FIX_OUTPUT
from dayone.store.atlas import AtlasStore

log = logging.getLogger(__name__)

LEASE_TTL_S = 10.0
LEASE_RENEW_S = 3.0
LEASE_POLL_S = 1.0
# A runner killed with -9 never releases its lease, so acquiring waits out one TTL before refusing.
LEASE_WAIT_S = LEASE_TTL_S + LEASE_POLL_S
MAX_PASSES = 5  # reconcile + confirm passes before giving up on checks that keep regressing
MAX_DRIFT_RESTARTS = 5  # restarts from t_clone within one pass before giving up on a moving branch
CLONE_STEP = "t_clone"
CLONE_HEAD_KEY = "local_head"  # t_clone's check puts the verified local HEAD sha here
MEMORY_SOURCES = frozenset({FixSource.EXACT_MEMORY, FixSource.SEMANTIC_MEMORY})


class LeaseHeld(RuntimeError):
    """Another runner holds this hire's lease."""


class LeaseLost(RuntimeError):
    """This runner's lease could not be renewed; stop before acting for a hire someone else may own."""


class HireNotFound(LookupError):
    pass


class MachineRequired(ValueError):
    pass


class MachineMismatch(ValueError):
    pass


class DriftUnsettled(RuntimeError):
    """The repository or the checks kept changing for longer than the engine's bounds."""


@dataclass(frozen=True)
class Parts:
    events: EventBus
    executor: Executor
    browser: BrowserSession
    gitea: Gitea
    memory: Memory
    steps: Sequence[Step]  # in STEP_IDS order
    fingerprint: Callable[[str], str]
    exec_timeout_s: float


OpenParts = Callable[[Hire], AbstractContextManager[Parts]]


@dataclass(frozen=True)
class ResetParts:
    reset_gitea: Callable[[str], None]  # delete the hire's SSH keys, untick their Welcome issue
    remove_container: Callable[[Hire], None]
    remove_browser_profile: Callable[[str], None]  # the saved sign-in would otherwise pass b_sign_in unrun


class Lease:
    """Held for a whole run; a background thread renews it, and `ensure()` stops the loop if it was lost."""

    def __init__(self, store: Store, hire: str, wait_s: float = LEASE_WAIT_S) -> None:
        self._store = store
        self._hire = hire
        self._wait_s = wait_s
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._renewer = threading.Thread(target=self._renew_loop, name=f"lease-{hire}", daemon=True)

    def __enter__(self) -> Lease:
        deadline = time.monotonic() + self._wait_s
        while not self._store.acquire_lease(self._hire, self.owner, LEASE_TTL_S):
            if time.monotonic() >= deadline:
                raise LeaseHeld(f"another runner holds the lease for {self._hire}")
            log.info("lease for %s is held; waiting for it to be released or expire", self._hire)
            time.sleep(LEASE_POLL_S)
        self._renewer.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._stop.set()
        self._renewer.join()
        if not self._lost.is_set():
            self._store.release_lease(self._hire, self.owner)

    def ensure(self) -> None:
        if self._lost.is_set():
            raise LeaseLost(f"lease for {self._hire} was lost")

    def _renew_loop(self) -> None:
        while not self._stop.wait(LEASE_RENEW_S):
            try:
                renewed = self._store.renew_lease(self._hire, self.owner, LEASE_TTL_S)
            except PyMongoError:
                log.exception("lease renewal for %s failed", self._hire)
                renewed = False
            if not renewed:
                self._lost.set()
                return


# --- entry points ---------------------------------------------------------------


def run_hire(name: str, machine: Machine | None, resume: bool) -> bool:
    """Start (new run_id) or resume (same run_id) a hire. True when a full confirm pass passed."""
    # Imported here: wiring pulls in Docker, Playwright and the LLM clients; the loop itself needs none of them.
    from dayone.engine import wiring

    config = settings()
    with wiring.open_store(config) as store:
        return drive(store, name, machine, resume, lambda hire: wiring.open_parts(config, store, hire))


def reset(name: str | None) -> None:
    """Return a hire (or every hire in Atlas) to day zero. Experiences are kept: they are the point."""
    from dayone.engine import wiring

    config = settings()
    with wiring.open_store(config) as store:
        reset_hires(store, name, wiring.reset_parts(config, store))


def reset_hires(store: AtlasStore, name: str | None, parts: ResetParts) -> list[str]:
    """Gitea keys and Welcome ticks, browser profile, container, then the Atlas ledger,
    so the next run's checks fail before they pass.

    Every lease is taken before anything is touched: a hire that is being run makes the whole reset refuse.
    """
    names = [name] if name else store.hire_names()
    owner = f"reset:{socket.gethostname()}:{os.getpid()}"
    taken: list[str] = []
    for hire_name in names:
        if not store.acquire_lease(hire_name, owner, LEASE_TTL_S):
            for held in taken:
                store.release_lease(held, owner)
            raise LeaseHeld(f"{hire_name} is being run; stop it before resetting")
        taken.append(hire_name)
    for hire_name in names:
        parts.reset_gitea(hire_name)
        parts.remove_browser_profile(hire_name)
        hire = store.get_hire(hire_name)
        if hire is not None:  # no hire doc means the engine never created a container for it
            parts.remove_container(hire)
        store.reset_hire(hire_name)  # deletes the hire doc, and with it the lease
    return names


def drive(store: Store, name: str, machine: Machine | None, resume: bool, open_parts: OpenParts) -> bool:
    with Lease(store, name) as lease:
        hire = _hire_for_run(store, name, machine, resume)
        store.upsert_hire(hire)
        with open_parts(hire) as parts:
            return Run(store, parts, hire, lease).execute(resume)


def _hire_for_run(store: Store, name: str, machine: Machine | None, resume: bool) -> Hire:
    if resume:
        existing = store.get_hire(name)
        if existing is None:
            raise HireNotFound(f"no run to resume for {name}")
        if machine is not None and machine != existing.machine:
            raise MachineMismatch(f"{name} was started on {existing.machine}, not {machine}")
        return existing
    if machine is None:
        raise MachineRequired(f"starting {name} needs a machine")
    return Hire(name=name, machine=machine, container=container_name(name), run_id=uuid.uuid4().hex)


# --- the loop -------------------------------------------------------------------


class Run:
    def __init__(self, store: Store, parts: Parts, hire: Hire, lease: Lease) -> None:
        self.store = store
        self.parts = parts
        self.hire = hire
        self.lease = lease
        self.events = parts.events
        self.clone_head: str | None = None  # HEAD verified by t_clone's latest passing check

    def execute(self, resume: bool) -> bool:
        self.events.emit(EventType.RUN_RESUMED if resume else EventType.RUN_STARTED, machine=self.hire.machine.value)
        self.parts.executor.ensure_container()
        ok = self._converge()
        self.store.finish_hire(self.hire.name, ok)
        self.events.emit(EventType.RUN_FINISHED if ok else EventType.RUN_FAILED)
        return ok

    def _converge(self) -> bool:
        for _ in range(MAX_PASSES):
            if not self._reconcile_pass():
                return False
            drifted = self._confirm_pass()
            if drifted is None:
                return True
            reason = f"{drifted} no longer passes its check"
            self.events.emit(EventType.DRIFT_DETECTED, drifted, reason=reason)  # the step is the event's step_id
            self._mark_stale_from(drifted, reason)
        raise DriftUnsettled(f"{self.hire.name}: steps kept drifting for {MAX_PASSES} passes")

    def _reconcile_pass(self) -> bool:
        steps = self.parts.steps
        index = 0
        restarts = 0
        while index < len(steps):
            self.lease.ensure()
            step = steps[index]
            if self._head_moved(step):
                restarts += 1
                if restarts > MAX_DRIFT_RESTARTS:
                    raise DriftUnsettled(f"{REPO} kept moving for {MAX_DRIFT_RESTARTS} restarts")
                index = self._index(CLONE_STEP)
                continue
            if not self._reconcile_step(step):
                return False
            index += 1
        return True

    def _confirm_pass(self) -> str | None:
        """Re-check every step; the first that no longer passes is drift. None when everything still holds."""
        for step in self.parts.steps:
            self.lease.ensure()
            self.events.emit(EventType.STEP_CHECKING, step.id, confirm=True)
            if not self._check(step).ok:
                return step.id
        return None

    def _head_moved(self, step: Step) -> bool:
        """Drift probe (Track 3 question 3): before each step after t_clone, has the remote branch moved?"""
        if self.clone_head is None or self._index(step.id) <= self._index(CLONE_STEP):
            return False
        head = self.parts.gitea.branch_head(REPO, REPO_BRANCH)
        if head == self.clone_head:
            return False
        self.events.emit(EventType.DRIFT_DETECTED, step.id, **{"from": self.clone_head, "to": head})
        self._mark_stale_from(CLONE_STEP, f"{REPO}@{REPO_BRANCH} moved {self.clone_head} -> {head}")
        return True

    def _mark_stale_from(self, step_id: str, reason: str) -> None:
        later = [step.id for step in self.parts.steps[self._index(step_id) :]]
        self.store.mark_stale(self.hire.name, later, reason)
        for stale in later:
            self.events.emit(
                EventType.STEP_STATUS, stale, status=StepStatus.STALE.value, evidence=reason, reconciled=False
            )
        if CLONE_STEP in later:
            self.clone_head = None

    def _index(self, step_id: str) -> int:
        ids = [step.id for step in self.parts.steps]
        return ids.index(step_id) if step_id in ids else len(ids)

    def _reconcile_step(self, step: Step) -> bool:
        self.events.emit(EventType.STEP_CHECKING, step.id)
        check = self._check(step)
        previous = self.store.steps(self.hire.name).get(step.id)
        if check.ok:
            if previous is not None and previous.status in DONE_STATUSES:
                self._report(step, previous.status, check, reconciled=True)
            elif previous is not None and previous.status == StepStatus.PENDING:
                # A crash landed between run() and its check: the effect took, so it is verified, not re-run.
                self._finish(step, StepStatus.VERIFIED, check, None, reconciled=True)
            else:
                self._finish(step, StepStatus.SKIPPED, check, None, reconciled=True)
            return True

        self._intend(step)
        ran, raised = self._run(step)
        check = self._check(step)
        if check.ok:
            self._finish(step, StepStatus.VERIFIED, check, None, reconciled=False)
            return True

        # An exception from run() is the most specific account of what went wrong, so it leads.
        error_text = ran.output if raised else check.error_text or ran.output or check.evidence
        return self._repair(step, error_text)

    def _repair(self, step: Step, error_text: str) -> bool:
        fingerprint = self.parts.fingerprint(error_text)
        self.events.emit(EventType.ERROR_CAPTURED, step.id, fingerprint=fingerprint, error_text=error_text)
        failure = Failure(
            hire=self.hire.name,
            step_id=step.id,
            kind=step.kind,
            error_text=error_text,
            fingerprint=fingerprint,
            facts=self.parts.executor.facts(),
        )
        resolution = self.parts.memory.resolve(failure, lambda proposal: self._try_fix(step, proposal))
        if resolution.fixed and resolution.proposal is not None:
            status = (
                StepStatus.FIXED_FROM_MEMORY
                if resolution.proposal.source in MEMORY_SOURCES
                else StepStatus.FIXED_BY_REASONING
            )
            self._finish(step, status, resolution.last_check, resolution, reconciled=False)
            return True
        self.store.mark_failed(self.hire.name, step.id, resolution.last_check)
        self._report(step, StepStatus.FAILED, resolution.last_check, reconciled=False)
        return False

    def _try_fix(self, step: Step, proposal: FixProposal) -> CheckResult:
        """Apply a proposal, then redo the step and return its independent check."""
        self.lease.ensure()
        executor = self.parts.executor
        self._intend(step)
        in_repo = executor.exec(f"test -d {REPO_DIR}", self.parts.exec_timeout_s).ok
        outputs: list[dict[str, object]] = []  # what each fix command printed, for memory's next attempt
        for command in proposal.commands:
            result = executor.exec(command, self.parts.exec_timeout_s, REPO_DIR if in_repo else None)
            outputs.append({"command": command, "exit_code": result.exit_code, "output": result.output})
            if not result.ok:
                failed = CheckResult(
                    ok=False,
                    evidence=f"fix command failed: {command}",
                    detail={FIX_OUTPUT: outputs},
                    error_text=result.output,
                )
                self.store.record_check(self.hire.name, step.id, failed)
                return failed
        self._run(step)
        check = self._check(step)
        return replace(check, detail={**check.detail, FIX_OUTPUT: outputs})

    def _intend(self, step: Step) -> None:
        key = idempotency_key(self.hire.name, step.id)
        record = self.store.mark_pending(self.hire.name, step.id, key)
        self.events.emit(EventType.STEP_PENDING, step.id, run_count=record.run_count, idempotency_key=key)

    def _run(self, step: Step) -> tuple[RunResult, bool]:
        """run() at the step boundary; an exception is a failed run whose message is the error. True if raised."""
        try:
            return step.run(self._context(step)), False
        # Broad on purpose: steps call Playwright, Docker and HTTP, and any failure there must reach repair.
        except Exception as error:  # noqa: BLE001
            return RunResult(ok=False, output=self._raised(step, "run", error)), True

    def _check(self, step: Step) -> CheckResult:
        try:
            check = step.check(self._context(step))
        # Broad on purpose: steps call Playwright, Docker and HTTP; a check that cannot run has not passed.
        except Exception as error:  # noqa: BLE001
            text = self._raised(step, "check", error)
            check = CheckResult(ok=False, evidence=f"check raised {type(error).__name__}", error_text=text)
        self.store.record_check(self.hire.name, step.id, check)
        self.events.emit(
            EventType.STEP_CHECK_RESULT, step.id, ok=check.ok, evidence=check.evidence, detail=check.detail
        )
        if check.ok and step.id == CLONE_STEP:
            self.clone_head = check.detail[CLONE_HEAD_KEY]
        return check

    def _raised(self, step: Step, phase: str, error: Exception) -> str:
        log.warning("%s.%s() raised", step.id, phase, exc_info=error)
        text = f"{type(error).__name__}: {error}"
        trace = "".join(traceback.format_exception(error))
        self.events.emit(EventType.TERMINAL_OUTPUT, step.id, command=f"{step.id}.{phase}()", chunk=trace)
        return text

    def _finish(
        self, step: Step, status: StepStatus, check: CheckResult, resolution: Resolution | None, reconciled: bool
    ) -> None:
        self.store.mark_done(self.hire.name, step.id, status, check, resolution)
        self._report(step, status, check, reconciled)
        item = WELCOME_CHECKLIST.get(step.id)
        if item is not None and not reconciled:
            self._tick(step, item)

    def _tick(self, step: Step, item: str) -> None:
        # Best effort: b_checklist is the check of record for the Welcome issue.
        try:
            self.parts.browser.tick_checklist(item)
        except PlaywrightError:
            log.warning("could not tick %r for %s; b_checklist will", item, step.id, exc_info=True)

    def _report(self, step: Step, status: StepStatus, check: CheckResult, reconciled: bool) -> None:
        self.events.emit(
            EventType.STEP_STATUS, step.id, status=status.value, evidence=check.evidence, reconciled=reconciled
        )

    def _context(self, step: Step) -> StepContext:
        return StepContext(
            hire=self.hire,
            step_id=step.id,
            idempotency_key=idempotency_key(self.hire.name, step.id),
            executor=self.parts.executor,
            browser=self.parts.browser,
            gitea=self.parts.gitea,
            events=self.events,
            memory=self.parts.memory,
        )
