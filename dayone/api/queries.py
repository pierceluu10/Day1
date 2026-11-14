"""Every number and status the page shows, as Atlas queries. QA's acceptance test 7 reuses these.

Reads only the shapes in `dayone.contracts`: `events` documents are `Event` fields, `steps` documents
are `StepRecord` fields plus `llm_calls`, `hires` documents are `Hire` fields keyed by `_id` = name (the engine's store). The definitions match
session 7's `tests/acceptance/atlas.py` (see the shared qa-atlas-fields note).
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
LIFECYCLE_TYPES = [EventType.RUN_STARTED.value, EventType.RUN_RESUMED.value, *RUN_END_TYPES]
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


def replay(db: Database, hire: str, terminal_lines: int) -> list[dict[str, Any]]:
    """A hire's history for a page that just opened: every event except old terminal output, of which only the
    chunks holding the last `terminal_lines` lines. In emit order; ends with the hire's newest event, so the
    live stream continues after it. The line count runs in Atlas: the skipped output never crosses the network."""
    newest = db[EVENTS].find_one({"hire": hire}, {"_id": 1}, sort=[("_id", DESCENDING)])
    if newest is None:
        return []
    upto = {"hire": hire, "_id": {"$lte": newest["_id"]}}
    terminal = EventType.TERMINAL_OUTPUT.value
    state = list(db[EVENTS].find({**upto, "type": {"$ne": terminal}}))
    tail = db[EVENTS].aggregate(
        [
            {"$match": {**upto, "type": terminal}},
            {"$set": {"_lines": {"$subtract": [{"$size": {"$split": ["$data.chunk", "\n"]}}, 1]}}},  # newlines in the chunk
            {
                "$setWindowFields": {
                    "sortBy": {"_id": -1},
                    "output": {"_newer_lines": {"$sum": "$_lines", "window": {"documents": ["unbounded", -1]}}},
                }
            },
            {"$match": {"$expr": {"$lt": [{"$ifNull": ["$_newer_lines", 0]}, terminal_lines]}}},
            {"$unset": ["_lines", "_newer_lines"]},
        ]
    )
    return sorted([*state, *tail], key=lambda doc: doc["_id"])


def steps(db: Database, hire: str) -> list[dict[str, Any]]:
    """All steps in run order; a step the ledger has not seen yet is `not_started`."""
    found = {doc["step_id"]: doc for doc in db[STEPS].find({"hire": hire}, {"_id": 0})}
    return [found.get(step_id, {"hire": hire, "step_id": step_id, "status": StepStatus.NOT_STARTED.value}) for step_id in STEP_IDS]


def latest_run_id(db: Database, hire: str) -> str | None:
    doc = db[EVENTS].find_one({"hire": hire, "type": EventType.RUN_STARTED.value}, sort=[("ts", DESCENDING), ("_id", DESCENDING)])
    return doc["run_id"] if doc else None


def run_summary(db: Database, hire: str, run_id: str | None = None) -> dict[str, Any] | None:
    """One run's line. elapsed: first RUN_STARTED to the end event (or now while unfinished), killed time included.
    errors: ERROR_CAPTURED events. fixes_from_memory and llm_calls: the hire's step ledger."""
    run_id = run_id or latest_run_id(db, hire)
    if run_id is None:
        return None
    match = {"hire": hire, "run_id": run_id}
    started = db[EVENTS].find_one({**match, "type": EventType.RUN_STARTED.value}, sort=[("ts", ASCENDING), ("_id", ASCENDING)])
    if started is None:
        return None
    # A failed run can be resumed: only the latest lifecycle event says whether it is over.
    last = db[EVENTS].find_one({**match, "type": {"$in": LIFECYCLE_TYPES}}, sort=[("ts", DESCENDING), ("_id", DESCENDING)])
    finished_at = last["ts"] if last["type"] in RUN_END_TYPES else None
    ledger = next(
        db[STEPS].aggregate(
            [
                {"$match": {"hire": hire}},
                {
                    "$group": {
                        "_id": None,
                        "fixes_from_memory": {"$sum": {"$cond": [{"$in": ["$fix_source", list(MEMORY_SOURCES)]}, 1, 0]}},
                        "llm_calls": {"$sum": {"$ifNull": ["$llm_calls", 0]}},
                    }
                },
            ]
        ),
        {"fixes_from_memory": 0, "llm_calls": 0},
    )
    hire_doc = db[HIRES].find_one({"_id": hire}, {"machine": 1}) or {}
    return {
        "hire": hire,
        "machine": hire_doc.get("machine"),
        "run_id": run_id,
        "started_at": started["ts"],
        "finished_at": finished_at,
        "ok": None if finished_at is None else last["type"] == EventType.RUN_FINISHED.value,
        "elapsed_s": round(((finished_at or datetime.now(UTC)) - started["ts"]).total_seconds(), 1),
        "errors": db[EVENTS].count_documents({**match, "type": EventType.ERROR_CAPTURED.value}),
        "fixes_from_memory": ledger["fixes_from_memory"],
        "llm_calls": ledger["llm_calls"],
    }


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
