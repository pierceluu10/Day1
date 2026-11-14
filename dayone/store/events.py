"""Event stream in Atlas `events`.

`emit` only appends to an in-memory batch; one writer thread inserts batches in emit order, so terminal output
costs one insert per flush interval instead of one per chunk, and a reader tailing by `ts` never skips an event.
A `kill -9` loses at most the last unflushed interval; the `steps` ledger, not this stream, is the state of record.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from dayone.contracts import Event, EventType, Hire
from dayone.store.atlas import EVENTS

FLUSH_INTERVAL_S = 0.2
TAIL_POLL_S = 0.25
TS_RESOLUTION = timedelta(milliseconds=1)  # BSON dates hold milliseconds


def _truncate(ts: datetime) -> datetime:
    return ts.replace(microsecond=ts.microsecond // 1000 * 1000)


class EventFlushError(RuntimeError):
    pass


class AtlasEventBus:
    def __init__(self, db: Database, hire: Hire) -> None:
        self._events = db[EVENTS]
        self._hire = hire
        self._lock = threading.Lock()  # guards the batch; emit never waits on the network
        self._write_lock = threading.Lock()  # serialises inserts so batches land in ts order
        self._batch: list[dict[str, Any]] = []
        self._seq = 0
        last = self._events.find_one({"hire": hire.name}, sort=[("ts", DESCENDING)], projection={"ts": True})
        # Continue strictly after whatever an earlier (possibly killed) process wrote for this hire.
        self._last_ts: datetime | None = last["ts"] if last else None
        self._error: PyMongoError | None = None
        self._stop = threading.Event()
        self._writer = threading.Thread(target=self._write_loop, name=f"events-{hire.name}", daemon=True)
        self._writer.start()

    def emit(self, type: EventType, step_id: str | None = None, **data: Any) -> None:
        if self._error is not None:
            raise EventFlushError("event writer failed") from self._error
        with self._lock:
            ts = _truncate(datetime.now(UTC))
            if self._last_ts is not None and ts <= self._last_ts:
                ts = self._last_ts + TS_RESOLUTION
            self._last_ts = ts
            self._seq += 1
            self._batch.append(
                {
                    "hire": self._hire.name,
                    "run_id": self._hire.run_id,
                    "type": type.value,
                    "ts": ts,
                    "seq": self._seq,
                    "step_id": step_id,
                    "data": data,
                }
            )

    def tail(self, hire: str, after: datetime | None) -> Iterator[Event]:
        while True:
            query: dict[str, Any] = {"hire": hire}
            if after is not None:
                query["ts"] = {"$gt": after}
            for doc in self._events.find(query).sort([("ts", ASCENDING), ("seq", ASCENDING)]):
                after = doc["ts"]
                yield Event(
                    hire=doc["hire"],
                    run_id=doc["run_id"],
                    type=EventType(doc["type"]),
                    ts=doc["ts"],
                    step_id=doc["step_id"],
                    data=doc["data"],
                )
            time.sleep(TAIL_POLL_S)

    def flush(self) -> None:
        with self._write_lock:
            with self._lock:
                batch, self._batch = self._batch, []
            if batch:
                self._events.insert_many(batch, ordered=True)

    def close(self) -> None:
        self._stop.set()
        self._writer.join()
        self.flush()
        if self._error is not None:
            raise EventFlushError("event writer failed") from self._error

    def _write_loop(self) -> None:
        while not self._stop.wait(FLUSH_INTERVAL_S):
            try:
                self.flush()
            except PyMongoError as error:
                self._error = error
                return
