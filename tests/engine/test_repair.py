"""A step whose naive run() is not enough: the failure goes to memory, the fix is verified by the step's check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from dayone.contracts import (
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
from dayone.memory.experience import FIX_OUTPUT
from dayone.store.atlas import AtlasStore
from dayone.store.events import AtlasEventBus
from tests.engine.fakes import FakeBrowser, FakeExecutor, FakeGitea, FakeMemory, fake_fingerprint, parts_opener

FIX_COMMAND = "apt-get install -y libpq-dev"
ERROR = "pg_config executable not found"
STEP_ID = "t_install_deps"


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
    executor = FakeExecutor()
    memory = FakeMemory(
        fixes=[
            FixProposal(commands=("false-start",), source=FixSource.REASONING, rationale="wrong"),
            FixProposal(commands=(FIX_COMMAND,), source=FixSource.SEMANTIC_MEMORY, rationale="apt", experience_id="e1"),
        ]
    )
    opener = parts_opener(store, [needs_fix_step(tmp_path, executor)], FakeGitea(), memory, FakeBrowser(), executor)
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
    assert doc["llm_calls"] == 0
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


SIGN_IN_ERROR = "Timeout 30000ms exceeded waiting for locator('#user_name')"


def raising_run_step(workdir: Path, executor: FakeExecutor) -> Step:
    marker = workdir / "signed_in"

    def run(context: StepContext) -> RunResult:
        if not any(command == FIX_COMMAND for command, _ in executor.commands):
            raise PlaywrightError(SIGN_IN_ERROR)
        marker.touch()
        return RunResult(ok=True, output="signed in")

    def check(context: StepContext) -> CheckResult:
        if marker.exists():
            return CheckResult(ok=True, evidence="API /user = ada")
        return CheckResult(ok=False, evidence="not signed in", error_text="GET /user -> 401")

    return Step(id="b_sign_in", kind=StepKind.BROWSER, title="sign in", check=check, run=run)


def test_exception_from_run_is_a_failed_run_that_reaches_memory(store: AtlasStore, tmp_path: Path) -> None:
    executor = FakeExecutor()
    memory = FakeMemory(fixes=[FixProposal(commands=(FIX_COMMAND,), source=FixSource.REASONING, rationale="retry")])
    opener = parts_opener(store, [raising_run_step(tmp_path, executor)], FakeGitea(), memory, FakeBrowser(), executor)
    assert drive(store, "raiser", Machine.UBUNTU, False, opener)

    (failure,) = memory.failures
    print("error text given to memory:", failure.error_text)
    assert failure.error_text == f"Error: {SIGN_IN_ERROR}", "the exception, not the check's 401, is the error"
    assert store.steps("raiser")["b_sign_in"].status == StepStatus.FIXED_BY_REASONING
    trace = store.db["events"].find_one(
        {"hire": "raiser", "type": "terminal_output", "data.command": "b_sign_in.run()"}
    )
    assert trace is not None and SIGN_IN_ERROR in trace["data"]["chunk"]


def test_exception_from_check_is_a_failed_check(store: AtlasStore, tmp_path: Path) -> None:
    marker = tmp_path / "done"

    def check(context: StepContext) -> CheckResult:
        if not marker.exists():
            raise ConnectionError("gitea refused connection")
        return CheckResult(ok=True, evidence="done present")

    def run(context: StepContext) -> RunResult:
        marker.touch()
        return RunResult(ok=True, output="")

    step = Step(id="t_flaky", kind=StepKind.TERMINAL, title="flaky", check=check, run=run)
    assert drive(
        store, "flaky", Machine.UBUNTU, False, parts_opener(store, [step], FakeGitea(), FakeMemory(), FakeBrowser())
    )
    doc = store.db["steps"].find_one({"hire": "flaky", "step_id": "t_flaky"})
    assert doc["status"] == StepStatus.VERIFIED and doc["run_count"] == 1
    assert doc["check_ever_failed"], "the raising check counts as the failure before the pass"


INSPECT = "grep -n DATABASE_URL settings.py"
INSPECT_OUTPUT = "12:DATABASE_URL = os.environ['DATABASE_URL']"
BROKEN = "python -m acme_api.init_db"
BROKEN_OUTPUT = "sqlite3.OperationalError: unable to open database file"


def test_try_fix_reports_each_fix_commands_output_on_both_returns(store: AtlasStore, tmp_path: Path) -> None:
    executor = FakeExecutor()
    executor.results = {INSPECT: (0, INSPECT_OUTPUT), BROKEN: (1, BROKEN_OUTPUT)}
    seen: list[CheckResult] = []

    class RecordingMemory(FakeMemory):
        def resolve(self, failure, try_fix):
            return super().resolve(failure, lambda proposal: seen.append(try_fix(proposal)) or seen[-1])

    memory = RecordingMemory(
        fixes=[
            FixProposal(commands=(INSPECT, BROKEN, "never-run"), source=FixSource.REASONING, rationale="inspect"),
            FixProposal(commands=(INSPECT, FIX_COMMAND), source=FixSource.REASONING, rationale="fix"),
        ]
    )
    opener = parts_opener(store, [needs_fix_step(tmp_path, executor)], FakeGitea(), memory, FakeBrowser(), executor)
    assert drive(store, "inspector", Machine.UBUNTU, False, opener)

    failed, passed = seen
    print("failed detail:", failed.detail, "\npassed detail:", passed.detail)
    assert not failed.ok and failed.error_text == BROKEN_OUTPUT
    assert failed.detail[FIX_OUTPUT] == [
        {"command": INSPECT, "exit_code": 0, "output": INSPECT_OUTPUT},
        {"command": BROKEN, "exit_code": 1, "output": BROKEN_OUTPUT},
    ], "stops at the first failing command and reports every command it ran"
    assert passed.ok and passed.evidence == "pip check: no broken requirements"
    assert passed.detail[FIX_OUTPUT] == [
        {"command": INSPECT, "exit_code": 0, "output": INSPECT_OUTPUT},
        {"command": FIX_COMMAND, "exit_code": 0, "output": ""},
    ]
