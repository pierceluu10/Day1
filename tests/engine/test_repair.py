"""A step whose naive run() is not enough: the failure goes to memory, the fix is verified by the step's check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from dayone.contracts import (
    WELCOME_CHECKLIST,
    CheckResult,
    EventType,
    FixProposal,
    FixSource,
    Hire,
    Machine,
    RunResult,
    Step,
    StepContext,
    StepKind,
    StepStatus,
)
from dayone.engine.runner import drive
from dayone.store.atlas import AtlasStore
from dayone.store.events import AtlasEventBus
from tests.engine.fakes import FakeBrowser, FakeExecutor, FakeGitea, FakeMemory, fake_fingerprint, parts_opener

FIX_COMMAND = "apt-get install -y libpq-dev"
ERROR = "pg_config executable not found"
STEP_ID = "t_install_deps"  # has a Welcome checklist item


def needs_fix_step(workdir: Path, executor: FakeExecutor) -> Step:
    marker = workdir / "deps.done"

    def run(context: StepContext) -> RunResult:
        if any(command == FIX_COMMAND for command, _ in executor.commands):
            marker.touch()
            return RunResult(ok=True, output="installed")
        return RunResult(ok=False, output=ERROR)

    def check(context: StepContext) -> CheckResult:
        if marker.exists():
            return CheckResult(ok=True, evidence="pip check: no broken requirements")
        return CheckResult(ok=False, evidence="import failed", error_text="")  # error only in run output

    return Step(id=STEP_ID, kind=StepKind.TERMINAL, title="deps", check=check, run=run)


def test_failure_is_fixed_from_memory_and_verified(store: AtlasStore, tmp_path: Path) -> None:
    executor, browser = FakeExecutor(), FakeBrowser()
    memory = FakeMemory(
        fixes=[
            FixProposal(commands=("false-start",), source=FixSource.REASONING, rationale="wrong"),
            FixProposal(commands=(FIX_COMMAND,), source=FixSource.SEMANTIC_MEMORY, rationale="apt", experience_id="e1"),
        ]
    )
    opener = parts_opener(store, [needs_fix_step(tmp_path, executor)], FakeGitea(), memory, browser, executor)
    assert drive(store, "fixer", Machine.UBUNTU, False, opener)

    (failure,) = memory.failures
    assert failure.error_text == ERROR, "run output is the error when the check has none"
    assert failure.fingerprint == fake_fingerprint(ERROR)
    record = store.steps("fixer")[STEP_ID]
    assert record.status == StepStatus.FIXED_FROM_MEMORY
    assert record.fix_source == FixSource.SEMANTIC_MEMORY and record.experience_id == "e1"
    assert record.run_count == 3, "first run + one per fix attempt, each with intent written first"
    doc = store.db["steps"].find_one({"hire": "fixer", "step_id": STEP_ID})
    assert doc["check_ever_failed"] and not doc["untrusted"]
    assert browser.ticked == [WELCOME_CHECKLIST[STEP_ID]]
    print("executor commands:", executor.commands)


def test_unfixable_step_fails_the_run(store: AtlasStore, tmp_path: Path) -> None:
    executor = FakeExecutor()
    opener = parts_opener(
        store, [needs_fix_step(tmp_path, executor)], FakeGitea(), FakeMemory(), FakeBrowser(), executor
    )
    assert not drive(store, "stuck", Machine.UBUNTU, False, opener)
    assert store.steps("stuck")[STEP_ID].status == StepStatus.FAILED
    assert store.db["hires"].find_one({"_id": "stuck"})["status"] == "failed"
    assert store.db["hires"].find_one({"_id": "stuck"})["lease_owner"] is None


def test_events_tail_in_emit_order_across_processes(store: AtlasStore) -> None:
    hire = Hire(name="tailer", machine=Machine.UBUNTU, container="dayone-tailer", run_id="r1")
    first = AtlasEventBus(store.db, hire)
    for index in range(50):
        first.emit(EventType.TERMINAL_OUTPUT, "t_base_tools", command="apt", chunk=str(index))
    first.close()
    second = AtlasEventBus(store.db, hire)  # a resumed process continues after the first's last ts
    second.emit(EventType.RUN_RESUMED)
    second.close()

    since = datetime.now(UTC) - timedelta(minutes=5)
    events = []
    for event in second.tail("tailer", since):
        events.append(event)
        if len(events) == 51:
            break
    assert [event.data.get("chunk") for event in events[:50]] == [str(index) for index in range(50)]
    assert events[-1].type == EventType.RUN_RESUMED
    assert all(a.ts < b.ts for a, b in zip(events, events[1:], strict=False)), "ts must be strictly increasing"
