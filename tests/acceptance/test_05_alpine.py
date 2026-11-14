"""Acceptance 5: on Alpine (never seen), at least one fix is retrieved semantically from an apt experience and adapted to apk."""

import json

import pytest

from atlas import find_experience, ledger, of_type, run_events
from dayone.contracts import DONE_STATUSES, STEP_IDS, EventType, FixSource, Machine, StepStatus
from harness import distinct, requires_system

pytestmark = [pytest.mark.system, requires_system]

SOURCE_MANAGER = "apt"
TARGET_MANAGER = "apk"


FIXED_STATUSES = frozenset({StepStatus.FIXED_FROM_MEMORY.value, StepStatus.FIXED_BY_REASONING.value})


def searchable(step_id: str) -> dict:
    return {"kind": "terminal", "outcome": "verified", "context.step_id": step_id, "embedding": {"$exists": True}}


def adapted_from(data: dict, manager: str) -> bool:
    """`adapted_from` names the source machine, e.g. "apt" or "ubuntu 22.04, apt"."""
    return manager in (data.get("adapted_from") or "").replace(",", " ").split()


def test_alpine_adapts_an_apt_fix_found_by_vector_search(onboard, db, hires):
    distinct(hires, "first", "alpine")
    training_hire, hire = hires["first"], hires["alpine"]
    training = onboard(training_hire, Machine.UBUNTU)
    assert training.exit_code == 0, training.output_tail
    donor_fixed = sorted(step_id for step_id, doc in ledger(db, training_hire).items() if doc["status"] in FIXED_STATUSES)
    unsearchable = [step_id for step_id in donor_fixed if not db.experiences.count_documents(searchable(step_id))]
    print(f"{training_hire} fixed {donor_fixed}")
    assert donor_fixed and unsearchable == [], f"no verified, embedded experience before {hire} starts for {unsearchable}"

    run = onboard(hire, Machine.ALPINE)
    assert run.exit_code == 0, run.output_tail

    steps = ledger(db, hire)
    errored = {event["step_id"] for event in of_type(run_events(db, hire, run.run_id), EventType.ERROR_CAPTURED)}
    not_from_memory = [
        f"{step_id}: {steps[step_id]['status']}"
        for step_id in sorted(errored & set(donor_fixed))
        if steps[step_id]["status"] != StepStatus.FIXED_FROM_MEMORY
    ]
    assert not_from_memory == [], f"{hire} hit errors {training_hire} had already fixed but did not fix them from memory"
    for step_id in STEP_IDS:
        assert steps[step_id]["status"] in DONE_STATUSES, (step_id, steps[step_id]["status"])

    hits = [
        event
        for event in of_type(run_events(db, hire, run.run_id), EventType.MEMORY_HIT)
        if event["data"].get("source") == FixSource.SEMANTIC_MEMORY and adapted_from(event["data"], SOURCE_MANAGER)
    ]
    for hit in hits:
        print(f"semantic hit {hit['step_id']}: {hit['data']}")
    assert hits, f"no semantic memory hit adapted from {SOURCE_MANAGER} in {hire}'s run"

    adapted = []
    for hit in hits:
        data = hit["data"]
        step = steps[hit["step_id"]]
        adaptation = find_experience(db, data["experience_id"])
        assert adaptation, f"memory_hit names experience {data['experience_id']}, not in Atlas"
        # The adapted fix is saved as a new experience that points at the apt one it came from.
        original = find_experience(db, str(adaptation["adapted_from_experience"]))
        assert original, f"adapted experience {adaptation['_id']} names no original in Atlas"
        assert isinstance(data["similarity"], float) and 0 < data["similarity"] <= 1, data
        assert data["source_hire"] == original["hire"] != hire, (data, original["hire"])
        assert SOURCE_MANAGER in json.dumps(original["context"], default=str), original["context"]
        assert TARGET_MANAGER in json.dumps(adaptation["context"], default=str), adaptation["context"]
        assert any(TARGET_MANAGER in command for command in data["commands"]), data["commands"]
        if step["status"] == StepStatus.FIXED_FROM_MEMORY and step["fix_source"] == FixSource.SEMANTIC_MEMORY:
            adapted.append(hit["step_id"])
    assert adapted, f"no adapted {SOURCE_MANAGER} fix is what finally fixed a step: {[h['step_id'] for h in hits]}"
