"""Atlas ledger. Every write touches one document with w="majority", so no transactions are needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError
from pymongo.write_concern import WriteConcern

from dayone.config import Settings
from dayone.contracts import CheckResult, FixSource, Hire, Machine, Resolution, StepRecord, StepStatus

HIRES = "hires"
STEPS = "steps"
EVENTS = "events"

HIRE_RUNNING = "running"
HIRE_FINISHED = "finished"
HIRE_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(UTC)


def _record(doc: dict[str, Any]) -> StepRecord:
    return StepRecord(
        hire=doc["hire"],
        step_id=doc["step_id"],
        status=StepStatus(doc["status"]),
        run_count=doc.get("run_count", 0),
        check_ever_failed=doc.get("check_ever_failed", False),
        evidence=doc.get("evidence", ""),
        updated_at=doc["updated_at"],
        fix_source=FixSource(doc["fix_source"]) if doc.get("fix_source") else None,
        experience_id=doc.get("experience_id"),
    )


class AtlasStore:
    def __init__(self, settings: Settings) -> None:
        self._client: MongoClient = MongoClient(settings.mongodb_uri, tz_aware=True)
        self.db: Database = self._client.get_database(settings.mongodb_db, write_concern=WriteConcern("majority"))
        self._hires = self.db[HIRES]
        self._steps = self.db[STEPS]
        self._steps.create_index([("hire", ASCENDING), ("step_id", ASCENDING)], unique=True)
        self.db[EVENTS].create_index([("hire", ASCENDING), ("ts", ASCENDING), ("seq", ASCENDING)])

    def close(self) -> None:
        self._client.close()

    # --- lease ----------------------------------------------------------------

    def acquire_lease(self, hire: str, owner: str, ttl_s: float) -> bool:
        now = _now()
        free = {"$or": [{"lease_owner": owner}, {"lease_owner": None}, {"lease_expires_at": {"$lte": now}}]}
        try:
            self._hires.find_one_and_update(
                {"_id": hire, **free},
                {"$set": {"lease_owner": owner, "lease_expires_at": now + timedelta(seconds=ttl_s)}},
                upsert=True,
            )
        except DuplicateKeyError:
            # The hire doc exists but the filter did not match: someone else holds a live lease.
            return False
        return True

    def renew_lease(self, hire: str, owner: str, ttl_s: float) -> bool:
        doc = self._hires.find_one_and_update(
            {"_id": hire, "lease_owner": owner},
            {"$set": {"lease_expires_at": _now() + timedelta(seconds=ttl_s)}},
        )
        return doc is not None

    def release_lease(self, hire: str, owner: str) -> None:
        self._hires.update_one(
            {"_id": hire, "lease_owner": owner}, {"$set": {"lease_owner": None, "lease_expires_at": None}}
        )

    # --- hires ----------------------------------------------------------------

    def upsert_hire(self, hire: Hire) -> None:
        self._hires.update_one(
            {"_id": hire.name},
            {
                "$set": {
                    "machine": hire.machine.value,
                    "container": hire.container,
                    "run_id": hire.run_id,
                    "status": HIRE_RUNNING,
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )

    def get_hire(self, name: str) -> Hire | None:
        doc = self._hires.find_one({"_id": name})
        if doc is None or "machine" not in doc:
            return None
        return Hire(name=name, machine=Machine(doc["machine"]), container=doc["container"], run_id=doc["run_id"])

    def finish_hire(self, hire: str, ok: bool) -> None:
        self._hires.update_one(
            {"_id": hire}, {"$set": {"status": HIRE_FINISHED if ok else HIRE_FAILED, "finished_at": _now()}}
        )

    def reset_hire(self, hire: str) -> None:
        self._steps.delete_many({"hire": hire})
        self.db[EVENTS].delete_many({"hire": hire})
        self._hires.delete_one({"_id": hire})

    # --- steps ----------------------------------------------------------------

    def steps(self, hire: str) -> dict[str, StepRecord]:
        return {doc["step_id"]: _record(doc) for doc in self._steps.find({"hire": hire})}

    def record_check(self, hire: str, step_id: str, result: CheckResult) -> None:
        now = _now()
        self._steps.update_one(
            {"hire": hire, "step_id": step_id},
            {
                "$set": {
                    "last_check": {
                        "ok": result.ok,
                        "evidence": result.evidence,
                        "detail": result.detail,
                        "error_text": result.error_text,
                        "at": now,
                    },
                    "updated_at": now,
                },
                # Booleans order false < true, so $max latches the flag once any check fails.
                "$max": {"check_ever_failed": not result.ok},
                "$setOnInsert": {"status": StepStatus.NOT_STARTED.value, "run_count": 0},
            },
            upsert=True,
        )

    def mark_pending(self, hire: str, step_id: str, idempotency_key: str) -> StepRecord:
        now = _now()
        doc = self._steps.find_one_and_update(
            {"hire": hire, "step_id": step_id},
            {
                "$set": {
                    "status": StepStatus.PENDING.value,
                    "idempotency_key": idempotency_key,
                    "intent_at": now,
                    "updated_at": now,
                },
                "$inc": {"run_count": 1},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return _record(doc)

    def mark_done(
        self, hire: str, step_id: str, status: StepStatus, check: CheckResult, resolution: Resolution | None
    ) -> StepRecord:
        proposal = resolution.proposal if resolution else None
        fields = {
            "status": status.value,
            "evidence": check.evidence,
            "evidence_detail": check.detail,
            "fix_source": proposal.source.value if proposal else None,
            "experience_id": proposal.experience_id if proposal else None,
            "llm_calls": resolution.llm_calls if resolution else 0,
            "updated_at": _now(),
        }
        # Pipeline update so `untrusted` is computed from the stored flag in the same single-document write;
        # $literal keeps user values (evidence detail) from being read as aggregation expressions.
        doc = self._steps.find_one_and_update(
            {"hire": hire, "step_id": step_id},
            [
                {
                    "$set": {
                        **{name: {"$literal": value} for name, value in fields.items()},
                        "untrusted": {"$not": [{"$ifNull": ["$check_ever_failed", False]}]},
                        "stale_reason": None,
                    }
                }
            ],
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return _record(doc)

    def mark_stale(self, hire: str, step_ids: list[str], reason: str) -> None:
        self._steps.update_many(
            {"hire": hire, "step_id": {"$in": step_ids}},
            {"$set": {"status": StepStatus.STALE.value, "stale_reason": reason, "updated_at": _now()}},
        )

    def mark_failed(self, hire: str, step_id: str, check: CheckResult) -> None:
        self._steps.update_one(
            {"hire": hire, "step_id": step_id},
            {
                "$set": {
                    "status": StepStatus.FAILED.value,
                    "evidence": check.evidence,
                    "evidence_detail": check.detail,
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )
