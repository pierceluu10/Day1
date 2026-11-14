"""Every number and status the page shows, as Atlas queries. QA's acceptance test 7 reuses these.

Reads only the shapes in `dayone.contracts`: `events` documents are `Event` fields, `steps` documents
are `StepRecord` fields, `hires` documents are `Hire` fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database

from dayone.config import Settings
from dayone.contracts import STEP_IDS, EventType, FixSource, StepStatus

EVENTS = "events"
STEPS = "steps"
HIRES = "hires"

RUN_END_TYPES = (EventType.RUN_FINISHED.value, EventType.RUN_FAILED.value)
MEMORY_SOURCES = (FixSource.EXACT_MEMORY.value, FixSource.SEMANTIC_MEMORY.value)


def connect(settings: Settings) -> tuple[MongoClient, Database]:
    client: MongoClient = MongoClient(settings.mongodb_uri, tz_aware=True)
    return client, client[settings.mongodb_db]


def events_after(db: Database, hire: str, after: ObjectId | None, limit: int) -> list[dict[str, Any]]:
    """Events for a hire in emit order. Keyed by `_id`, not `ts`: steps emit within the same millisecond."""
    query: dict[str, Any] = {"hire": hire}
    if after is not None:
        query["_id"] = {"$gt": after}
    return list(db[EVENTS].find(query).sort("_id", ASCENDING).limit(limit))


def steps(db: Database, hire: str) -> list[dict[str, Any]]:
    """All steps in run order; a step the ledger has not seen yet is `not_started`."""
    found = {doc["step_id"]: doc for doc in db[STEPS].find({"hire": hire}, {"_id": 0})}
    return [found.get(step_id, {"hire": hire, "step_id": step_id, "status": StepStatus.NOT_STARTED.value}) for step_id in STEP_IDS]


def latest_run_id(db: Database, hire: str) -> str | None:
    doc = db[EVENTS].find_one({"hire": hire, "type": EventType.RUN_STARTED.value}, sort=[("_id", DESCENDING)])
    return doc["run_id"] if doc else None


def run_summary(db: Database, hire: str, run_id: str | None = None) -> dict[str, Any] | None:
    """One run's line: elapsed wall time (start to finish, or to now while unfinished), errors, fixes, LLM calls."""
    run_id = run_id or latest_run_id(db, hire)
    if run_id is None:
        return None
    match = {"hire": hire, "run_id": run_id}
    counts = next(
        db[EVENTS].aggregate(
            [
                {"$match": match},
                {
                    "$group": {
                        "_id": None,
                        "started_at": {"$min": {"$cond": [{"$eq": ["$type", EventType.RUN_STARTED.value]}, "$ts", None]}},
                        "finished_at": {"$max": {"$cond": [{"$in": ["$type", list(RUN_END_TYPES)]}, "$ts", None]}},
                        "failed": {"$max": {"$eq": ["$type", EventType.RUN_FAILED.value]}},
                        "resumes": {"$sum": {"$cond": [{"$eq": ["$type", EventType.RUN_RESUMED.value]}, 1, 0]}},
                        "errors": {"$sum": {"$cond": [{"$eq": ["$type", EventType.ERROR_CAPTURED.value]}, 1, 0]}},
                        "fixes_from_memory": {"$sum": {"$cond": [_fix_attempt_from(MEMORY_SOURCES, ok_only=True), 1, 0]}},
                        "llm_fix_calls": {"$sum": {"$cond": [_fix_attempt_from((FixSource.REASONING.value,)), 1, 0]}},
                    }
                },
            ]
        ),
        None,
    )
    if counts is None or counts["started_at"] is None:
        return None
    hire_doc = db[HIRES].find_one({"name": hire}, {"machine": 1}) or {}
    end = counts["finished_at"] or datetime.now(UTC)
    return {
        "hire": hire,
        "machine": hire_doc.get("machine"),
        "run_id": run_id,
        "started_at": counts["started_at"],
        "finished_at": counts["finished_at"],
        "ok": None if counts["finished_at"] is None else not counts["failed"],
        "elapsed_s": round((end - counts["started_at"]).total_seconds(), 1),
        "resumes": counts["resumes"],
        "errors": counts["errors"],
        "fixes_from_memory": counts["fixes_from_memory"],
        "llm_fix_calls": counts["llm_fix_calls"],
    }


def _fix_attempt_from(sources: tuple[str, ...], ok_only: bool = False) -> dict[str, Any]:
    conditions: list[Any] = [{"$eq": ["$type", EventType.FIX_ATTEMPT.value]}, {"$in": ["$data.source", list(sources)]}]
    if ok_only:
        conditions.append({"$eq": ["$data.ok", True]})
    return {"$and": conditions}


def first_finished_hire(db: Database) -> tuple[str, str] | None:
    """(hire, run_id) of the earliest run that finished successfully: "hire 1" on the comparison line."""
    doc = db[EVENTS].find_one({"type": EventType.RUN_FINISHED.value}, sort=[("ts", ASCENDING), ("_id", ASCENDING)])
    return (doc["hire"], doc["run_id"]) if doc else None


def compare(db: Database, hire: str) -> dict[str, Any]:
    first = first_finished_hire(db)
    return {
        "first": run_summary(db, *first) if first else None,
        "current": run_summary(db, hire),
    }


def hires(db: Database) -> list[dict[str, Any]]:
    """Every hire with at least one run, oldest run first."""
    summaries = [run_summary(db, name) for name in db[EVENTS].distinct("hire", {"type": EventType.RUN_STARTED.value})]
    return sorted((s for s in summaries if s), key=lambda s: s["started_at"])


def latest_unfinished_hire(db: Database) -> str | None:
    """The hire whose most recent run started last and has no RUN_FINISHED/RUN_FAILED."""
    unfinished = [s for s in hires(db) if s["finished_at"] is None]
    return unfinished[-1]["hire"] if unfinished else None
