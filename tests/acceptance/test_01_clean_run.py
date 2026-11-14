"""Acceptance 1: an empty Ubuntu container reaches passing tests with every step verified and evidence stored."""

import re
from collections import Counter

import pytest

from atlas import find_experience, ledger, of_type, run_events
from dayone.contracts import DONE_STATUSES, REPO, REPO_BRANCH, REPO_DIR, STEP_IDS, EventType, FixSource, Machine, StepStatus
from harness import container_exec, requires_system

pytestmark = [pytest.mark.system, requires_system]

PASSED_COUNT = re.compile(r"(\d+) passed")
# Each earlier step ticks its own Welcome item, so by its turn b_checklist's check already passes.
TICKED_BY_EARLIER_STEPS = frozenset({"b_checklist"})
FIXED_STATUSES = frozenset({StepStatus.FIXED_FROM_MEMORY.value, StepStatus.FIXED_BY_REASONING.value})
REASONED = {StepStatus.FIXED_BY_REASONING.value: FixSource.REASONING.value}


def first_index(events: list[dict], type: EventType, step_id: str) -> int | None:
    return next((i for i, event in enumerate(events) if event["type"] == type.value and event.get("step_id") == step_id), None)


def test_empty_ubuntu_container_reaches_passing_tests(onboard, db, gitea, hires):
    hire = hires["first"]
    run = onboard(hire, Machine.UBUNTU)
    assert run.exit_code == 0, run.output_tail

    steps = ledger(db, hire)
    for step_id in STEP_IDS:
        doc = steps.get(step_id, {})
        print(f"{step_id:18} {doc.get('status')!s:20} runs={doc.get('run_count')} fix={doc.get('fix_source')} | {doc.get('evidence')}")
    assert sorted(steps) == sorted(STEP_IDS)

    problems = []
    for step_id in STEP_IDS:
        doc = steps[step_id]
        if doc["status"] not in DONE_STATUSES:
            problems.append(f"{step_id}: status {doc['status']}")
        if not doc["evidence"].strip():
            problems.append(f"{step_id} has no evidence")
        if step_id in TICKED_BY_EARLIER_STEPS:
            continue
        if doc["status"] == StepStatus.SKIPPED:
            problems.append(f"{step_id} passed its check on an empty container: the check is wrong")
        if doc["check_ever_failed"] is not True:
            problems.append(f"{step_id} never failed its check for {hire}: untrusted")
        if doc["status"] == StepStatus.VERIFIED and doc["run_count"] != 1:
            problems.append(f"{step_id} verified first try but run_count {doc['run_count']}")
        if doc["status"] in REASONED and doc["fix_source"] != REASONED[doc["status"]]:
            problems.append(f"{step_id}: {doc['status']} with fix_source {doc['fix_source']}")
        if doc.get("fix_source") == FixSource.EXACT_MEMORY:
            problems.append(f"{step_id} claims an exact memory hit with empty memory")
    assert problems == []

    match = PASSED_COUNT.search(steps["t_run_tests"]["evidence"])
    assert match and int(match.group(1)) > 0, steps["t_run_tests"]["evidence"]

    # Independent of the agent: the container's checkout is at the API's branch HEAD.
    local = container_exec(hire, f"git -C {REPO_DIR} rev-parse HEAD")
    assert local.returncode == 0, local.stderr
    assert local.stdout.strip() == gitea.branch_head(REPO, REPO_BRANCH)

    events = run_events(db, hire, run.run_id)
    # Every fix attempt writes intent again, so a fixed step's run_count is 1 + its attempts.
    pending_counts = Counter(event["step_id"] for event in of_type(events, EventType.STEP_PENDING))
    for step_id in STEP_IDS:
        assert steps[step_id]["run_count"] == pending_counts[step_id], (
            f"{step_id}: run_count {steps[step_id]['run_count']} but {pending_counts[step_id]} step_pending events"
        )
    assert events[0]["type"] == EventType.RUN_STARTED
    assert events[-1]["type"] == EventType.RUN_FINISHED
    for step_id in STEP_IDS:
        checked = first_index(events, EventType.STEP_CHECK_RESULT, step_id)
        pending = first_index(events, EventType.STEP_PENDING, step_id)
        final = first_index(events, EventType.STEP_STATUS, step_id)
        if step_id in TICKED_BY_EARLIER_STEPS:
            assert checked is not None and final is not None and checked < final, (step_id, checked, final)
            continue
        assert checked is not None and pending is not None and final is not None, (step_id, checked, pending, final)
        assert checked < pending < final, f"{step_id}: check {checked}, pending {pending}, status {final}"

    fixed = [step_id for step_id in STEP_IDS if steps[step_id]["status"] in FIXED_STATUSES]
    saved = {event["step_id"] for event in of_type(events, EventType.EXPERIENCE_SAVED)}
    assert set(fixed) <= saved, f"fixed without a saved experience: {set(fixed) - saved}"
    for step_id in fixed:
        experience_id = steps[step_id].get("experience_id")
        assert experience_id, f"{step_id} was fixed but its ledger entry names no experience"
        experience = find_experience(db, experience_id)
        assert experience, f"{step_id}: experience {experience_id} not in Atlas"
        assert experience["hire"] == hire and experience["outcome"] == "verified" and experience["verified_by_check"] is True, experience
