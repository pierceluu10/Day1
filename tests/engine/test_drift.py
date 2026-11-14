"""Track 3 question 3: the remote branch moves, or a verified check regresses, mid-run."""

from __future__ import annotations

from pathlib import Path

from dayone.contracts import CheckResult, Machine, RunResult, Step, StepContext, StepKind, StepStatus
from dayone.engine.runner import CLONE_HEAD_KEY, CLONE_STEP, drive
from dayone.store.atlas import AtlasStore
from tests.engine.fakes import FakeBrowser, FakeGitea, FakeMemory, log_line, log_lines, marker_step, parts_opener

NEW_HEAD = "b" * 40


def clone_step(workdir: Path, gitea: FakeGitea) -> Step:
    local = workdir / "local_head"

    def run(context: StepContext) -> RunResult:
        log_line(workdir, f"run {CLONE_STEP}")
        local.write_text(gitea.head)
        return RunResult(ok=True, output="cloned")

    def check(context: StepContext) -> CheckResult:
        log_line(workdir, f"check {CLONE_STEP}")
        head = local.read_text() if local.exists() else ""
        ok = head == gitea.head
        return CheckResult(ok=ok, evidence=f"local {head[:7]} vs API {gitea.head[:7]}", detail={CLONE_HEAD_KEY: head})

    return Step(id=CLONE_STEP, kind=StepKind.TERMINAL, title="clone", check=check, run=run)


def side_effect_step(base: Step, effect) -> Step:
    def run(context: StepContext) -> RunResult:
        result = base.run(context)
        effect()
        return result

    return Step(id=base.id, kind=base.kind, title=base.title, check=base.check, run=run)


def _runs(workdir: Path) -> list[str]:
    return [line.rsplit(" ", 1)[0] for line in log_lines(workdir) if line.startswith("run ")]


def _drift_events(store: AtlasStore, hire: str) -> list[dict]:
    return list(store.db["events"].find({"hire": hire, "type": "drift_detected"}).sort([("ts", 1), ("seq", 1)]))


def test_remote_head_moving_restarts_from_clone(store: AtlasStore, tmp_path: Path) -> None:
    gitea = FakeGitea()
    old_head = gitea.head

    def push_change() -> None:
        if gitea.head == old_head:
            gitea.head = NEW_HEAD

    steps = [
        marker_step(tmp_path, "s_before"),
        clone_step(tmp_path, gitea),
        side_effect_step(marker_step(tmp_path, "s_deps"), push_change),
        marker_step(tmp_path, "s_tests"),
    ]
    assert drive(store, "drift", Machine.UBUNTU, False, parts_opener(store, steps, gitea, FakeMemory(), FakeBrowser()))

    runs = _runs(tmp_path)
    print("runs:", runs)
    assert runs == ["run s_before", f"run {CLONE_STEP}", "run s_deps", f"run {CLONE_STEP}", "run s_tests"]
    drift = _drift_events(store, "drift")
    print("drift events:", [event["data"] for event in drift])
    assert [event["data"] for event in drift] == [{"from": old_head, "to": NEW_HEAD}]
    assert drift[0]["step_id"] == "s_tests"

    ledger = store.steps("drift")
    assert ledger[CLONE_STEP].status == StepStatus.VERIFIED and ledger[CLONE_STEP].run_count == 2
    assert ledger["s_deps"].status == StepStatus.SKIPPED, "re-verified by check after drift, not re-run"
    assert ledger["s_before"].run_count == 1 and ledger["s_deps"].run_count == 1
    stale = store.db["steps"].find_one({"hire": "drift", "step_id": "s_deps"})
    assert stale["stale_reason"] is None


def test_regressed_check_in_confirm_pass_is_reconciled(store: AtlasStore, tmp_path: Path) -> None:
    gitea = FakeGitea()
    first = marker_step(tmp_path, "s_first")
    breaks_first = side_effect_step(
        marker_step(tmp_path, "s_second"), lambda: (tmp_path / "s_first.done").unlink(missing_ok=True)
    )
    # Only the first run of s_second breaks s_first; afterwards s_second is checked, not re-run.
    assert drive(
        store,
        "regress",
        Machine.UBUNTU,
        False,
        parts_opener(store, [first, breaks_first], gitea, FakeMemory(), FakeBrowser()),
    )

    runs = _runs(tmp_path)
    print("runs:", runs)
    assert runs == ["run s_first", "run s_second", "run s_first"]
    drift = _drift_events(store, "regress")
    assert [(event["step_id"], "reason" in event["data"]) for event in drift] == [("s_first", True)]
    ledger = store.steps("regress")
    assert ledger["s_first"].status == StepStatus.VERIFIED and ledger["s_first"].run_count == 2
    # Marked stale with everything after s_first, then re-verified by its check without a second run.
    assert ledger["s_second"].status == StepStatus.SKIPPED and ledger["s_second"].run_count == 1
