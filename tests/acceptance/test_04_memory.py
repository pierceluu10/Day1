"""Acceptance 4: hire 2 on a fresh container hits hire 1's errors and resolves each from memory, with zero LLM calls."""

import pytest

from atlas import ledger, numbers, of_type, run_events
from dayone.contracts import DONE_STATUSES, EventType, FixSource, Machine, StepKind
from harness import distinct, requires_system

pytestmark = [pytest.mark.system, requires_system]


def errors_by_step(db, hire: str, run_id: str) -> dict[str, str]:
    """fingerprint -> step id of each error the run captured."""
    return {event["data"]["fingerprint"]: event["step_id"] for event in of_type(run_events(db, hire, run_id), EventType.ERROR_CAPTURED)}


def test_second_hire_resolves_first_hires_errors_from_memory(onboard, db, hires):
    distinct(hires, "first", "second")
    first_hire, second_hire = hires["first"], hires["second"]
    first = onboard(first_hire, Machine.UBUNTU)
    assert first.exit_code == 0, first.output_tail
    second = onboard(second_hire, Machine.UBUNTU)
    assert second.exit_code == 0, second.output_tail

    learned = {doc["fingerprint"] for doc in db.experiences.find({"hire": first_hire, "outcome": "verified", "kind": StepKind.TERMINAL.value})}
    first_errors = errors_by_step(db, first_hire, first.run_id)
    second_errors = errors_by_step(db, second_hire, second.run_id)
    print(f"{first_hire} errors: {first_errors}\n{second_hire} errors: {second_errors}\nlearned from {first_hire}: {learned}")
    assert first_errors, f"{first_hire} hit no errors: the demo repo's snags did not fire"
    assert set(first_errors) <= set(second_errors), f"{second_hire} did not hit {set(first_errors) - set(second_errors)}"

    steps = ledger(db, second_hire)
    events = run_events(db, second_hire, second.run_id)
    reasoned = {event["step_id"] for event in of_type(events, EventType.REASONING_STARTED)}
    for fingerprint in sorted(set(second_errors) & learned):
        step_id = second_errors[fingerprint]
        doc = steps[step_id]
        assert doc["status"] in DONE_STATUSES, (step_id, doc["status"])
        assert doc["fix_source"] == FixSource.EXACT_MEMORY, (step_id, doc["fix_source"])
        assert doc["llm_calls"] == 0, (step_id, doc["llm_calls"])
        assert step_id not in reasoned, f"{step_id}: reasoning started although {first_hire}'s fix matched exactly"

    first_numbers, second_numbers = numbers(db, first_hire), numbers(db, second_hire)
    print(f"{first_numbers}\n{second_numbers}")
    assert second_numbers.fixes_from_memory >= len(set(second_errors) & learned)
    assert second_numbers.elapsed_s < first_numbers.elapsed_s, "hire 2 was not faster than hire 1"
