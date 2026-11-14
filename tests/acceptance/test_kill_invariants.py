"""The kill harness's own invariant checks must catch the failures they exist for (synthetic event logs)."""

from dayone.contracts import EventType, StepStatus
from test_02_kill_resume import check_before_rerun, mask, reruns_after_done


def event(type: EventType, step_id: str | None = None, **data) -> dict:
    return {"type": type.value, "step_id": step_id, "data": data}


DONE = event(EventType.STEP_STATUS, "t_runtime", status=StepStatus.VERIFIED.value)
RERUN = event(EventType.STEP_PENDING, "t_runtime")


def test_rerun_of_a_done_step_is_a_violation():
    assert len(reruns_after_done([RERUN, DONE, RERUN])) == 1


def test_rerun_after_head_drift_is_allowed_for_downstream_steps():
    drift = event(EventType.DRIFT_DETECTED, **{"from": "3f2a1c9", "to": "8be04d1"})
    assert reruns_after_done([RERUN, DONE, drift, RERUN]) == []


def test_rerun_after_drift_is_not_allowed_upstream_of_the_clone():
    done = event(EventType.STEP_STATUS, "t_base_tools", status=StepStatus.VERIFIED.value)
    drift = event(EventType.DRIFT_DETECTED, **{"from": "a", "to": "b"})
    assert len(reruns_after_done([done, drift, event(EventType.STEP_PENDING, "t_base_tools")])) == 1


def test_stale_status_allows_a_rerun():
    stale = event(EventType.STEP_STATUS, "t_runtime", status=StepStatus.STALE.value)
    assert reruns_after_done([DONE, stale, RERUN]) == []


def test_pending_step_must_be_checked_before_rerun():
    checked = event(EventType.STEP_CHECK_RESULT, "t_runtime")
    assert check_before_rerun([checked, RERUN], "t_runtime") is None
    assert "re-run before" in check_before_rerun([RERUN, checked], "t_runtime")
    assert "never checked" in check_before_rerun([RERUN], "t_runtime")


def test_mask_hides_only_volatile_parts():
    assert mask("API lists key SHA256:abc+/9=; 14 passed in 0.52s") == mask("API lists key SHA256:zzz; 14 passed in 1.9s")
    assert mask("14 passed") != mask("13 passed")
    assert mask("local HEAD 3f2a1c9") != mask("local HEAD 8be04d1")
