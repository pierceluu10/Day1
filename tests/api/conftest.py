"""API tests run against real Atlas, in a throwaway database whose collections are dropped afterwards."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pymongo.database import Database

from dayone.api import queries
from dayone.config import settings

T0 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def atlas() -> Iterator[Database]:
    client, _ = queries.connect(settings())
    db = client[f"dayone_test_api_{uuid.uuid4().hex[:8]}"]
    yield db
    for name in (queries.EVENTS, queries.STEPS, queries.HIRES):
        db.drop_collection(name)
    client.close()


@pytest.fixture
def db(atlas: Database) -> Database:
    for name in (queries.EVENTS, queries.STEPS, queries.HIRES):
        atlas[name].delete_many({})
    return atlas


@pytest.fixture
def event() -> Callable[..., dict[str, Any]]:
    """An `events` document as the engine's EventBus writes it, `seconds` after a fixed start."""

    def make(hire: str, run_id: str, type: str, seconds: float, step_id: str | None = None, **data: Any) -> dict[str, Any]:
        return {"hire": hire, "run_id": run_id, "type": type, "ts": T0 + timedelta(seconds=seconds), "step_id": step_id, "data": data}

    return make
