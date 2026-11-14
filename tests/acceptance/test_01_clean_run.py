"""Acceptance 1: an empty Ubuntu container reaches passing tests with every step verified and evidence stored."""

import re

import pytest

from atlas import find_experience, ledger, of_type, run_events
from dayone.contracts import DONE_STATUSES, REPO, REPO_BRANCH, REPO_DIR, STEP_IDS, EventType, FixSource, Machine, StepStatus
from harness import container_exec, requires_system

pytestmark = [pytest.mark.system, requires_system]

HIRE = "ada"
PASSED_COUNT = re.compile(r"(\d+) passed")
REASONED = {StepStatus.FIXED_BY_REASONING.value: FixSource.REASONING.value}


def first_index(events: list[dict], type: EventType, step_id: str) -> int | None:
    return next((i for i, event in enumerate(events) if event["type"] == type.value and event.get("step_id") == step_id), None)


def test_empty_ubuntu_container_reaches_passing_tests(onboard, db, gitea):
    run = onboard(HIRE, Machine.UBUNTU)
    assert run.exit_code == 0, run.output_tail

    steps = ledger(db, HIRE)
    for step_id in STEP_IDS:
        doc = steps.get(step_id, {})
        print(f"{step_id:18} {doc.get('status')!s:20} runs={doc.get('run_count')} fix={doc.get('fix_source')} | {doc.get('evidence')}")
    assert sorted(steps) == sorted(STEP_IDS)

    for step_id in STEP_IDS:
        doc = steps[step_id]
        assert doc["status"] in DONE_STATUSES, f"{step_id}: {doc['status']}"
        assert doc["status"] != StepStatus.SKIPPED, f"{step_id} passed its check on an empty container: the check is wrong"
        assert doc["evidence"].strip(), f"{step_id} has no evidence"
        assert doc["check_ever_failed"] is True, f"{step_id} never failed its check for {HIRE}: untrusted"
        assert doc["run_count"] == 1, f"{step_id} run_count {doc['run_count']}"
        if doc["status"] in REASONED:
            assert doc["fix_source"] == REASONED[doc["status"]], f"{step_id}: {doc['fix_source']}"
        assert doc.get("fix_source") != FixSource.EXACT_MEMORY, f"{step_id} claims an exact memory hit with empty memory"

    match = PASSED_COUNT.search(steps["t_run_tests"]["evidence"])
    assert match and int(match.group(1)) > 0, steps["t_run_tests"]["evidence"]

    # Independent of the agent: the container's checkout is at the API's branch HEAD.
    local = container_exec(HIRE, f"git -C {REPO_DIR} rev-parse HEAD")
    assert local.returncode == 0, local.stderr
    assert local.stdout.strip() == gitea.branch_head(REPO, REPO_BRANCH)

    events = run_events(db, HIRE, run.run_id)
    assert events[0]["type"] == EventType.RUN_STARTED
    assert events[-1]["type"] == EventType.RUN_FINISHED
    for step_id in STEP_IDS:
        checked = first_index(events, EventType.STEP_CHECK_RESULT, step_id)
        pending = first_index(events, EventType.STEP_PENDING, step_id)
        final = first_index(events, EventType.STEP_STATUS, step_id)
        assert checked is not None and pending is not None and final is not None, (step_id, checked, pending, final)
        assert checked < pending < final, f"{step_id}: check {checked}, pending {pending}, status {final}"

    fixed = [step_id for step_id in STEP_IDS if steps[step_id]["status"] != StepStatus.VERIFIED]
    saved = {event["step_id"] for event in of_type(events, EventType.EXPERIENCE_SAVED)}
    assert set(fixed) <= saved, f"fixed without a saved experience: {set(fixed) - saved}"
    for step_id in fixed:
        experience_id = steps[step_id].get("experience_id")
        assert experience_id, f"{step_id} was fixed but its ledger entry names no experience"
        experience = find_experience(db, experience_id)
        assert experience, f"{step_id}: experience {experience_id} not in Atlas"
        assert experience["hire"] == HIRE and experience["outcome"] == "verified" and experience["verified_by_check"] is True, experience
