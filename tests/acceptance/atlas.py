"""What the acceptance tests and measure.py read from Atlas, and how the shown numbers are computed.

Field names follow `StepRecord` and `Event` in dayone/contracts.py, plus `steps.llm_calls`
(the finishing pass's `Resolution.llm_calls`). Every number the README, DEMO and API show is
defined here once; test 7 compares the API against these functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database

from dayone.contracts import EventType, FixSource

EVENT_ORDER = [("ts", 1), ("_id", 1)]
MEMORY_SOURCES = frozenset({FixSource.EXACT_MEMORY.value, FixSource.SEMANTIC_MEMORY.value})


class NoRun(LookupError):
    pass


def latest_run_id(db: Database, hire: str) -> str:
    event = db.events.find_one({"hire": hire, "type": EventType.RUN_STARTED.value}, sort=[("ts", -1), ("_id", -1)])
    if event is None:
        raise NoRun(f"no {EventType.RUN_STARTED} event for {hire!r} in database {db.name!r}")
    return event["run_id"]


def run_events(db: Database, hire: str, run_id: str) -> list[dict[str, Any]]:
    return list(db.events.find({"hire": hire, "run_id": run_id}).sort(EVENT_ORDER))


def ledger(db: Database, hire: str) -> dict[str, dict[str, Any]]:
    return {doc["step_id"]: doc for doc in db.steps.find({"hire": hire})}


def find_experience(db: Database, experience_id: str) -> dict[str, Any] | None:
    """StepRecord.experience_id is a string; the experience's _id may be that string or an ObjectId."""
    try:
        candidates: list[Any] = [experience_id, ObjectId(experience_id)]
    except InvalidId:
        candidates = [experience_id]
    return db.experiences.find_one({"_id": {"$in": candidates}})


def of_type(events: list[dict[str, Any]], type: EventType) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == type.value]


def hires_by_first_run(db: Database) -> list[str]:
    pipeline = [
        {"$match": {"type": EventType.RUN_STARTED.value}},
        {"$group": {"_id": "$hire", "first": {"$min": "$ts"}}},
        {"$sort": {"first": 1}},
    ]
    return [row["_id"] for row in db.events.aggregate(pipeline)]


@dataclass(frozen=True)
class HireNumbers:
    hire: str
    run_id: str
    elapsed_s: float | None  # None until the run has finished; includes time spent killed
    errors: int
    fixes_from_memory: int
    llm_calls: int


def numbers(db: Database, hire: str) -> HireNumbers:
    run_id = latest_run_id(db, hire)
    events = run_events(db, hire, run_id)
    started = of_type(events, EventType.RUN_STARTED)[0]["ts"]
    finished = of_type(events, EventType.RUN_FINISHED)
    steps = ledger(db, hire).values()
    missing_llm_calls = [doc["step_id"] for doc in steps if "llm_calls" not in doc]
    if missing_llm_calls:
        raise KeyError(f"steps docs for {hire!r} lack llm_calls: {missing_llm_calls}")
    return HireNumbers(
        hire=hire,
        run_id=run_id,
        elapsed_s=(finished[-1]["ts"] - started).total_seconds() if finished else None,
        errors=len(of_type(events, EventType.ERROR_CAPTURED)),
        fixes_from_memory=sum(doc.get("fix_source") in MEMORY_SOURCES for doc in steps),
        llm_calls=sum(doc["llm_calls"] for doc in steps),
    )
