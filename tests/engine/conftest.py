from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator

import pytest

from dayone.config import Settings, settings
from dayone.store.atlas import AtlasStore

TEST_DB_PREFIX = "dayone_test_engine_"


@pytest.fixture
def test_settings() -> Settings:
    return dataclasses.replace(settings(), mongodb_db=f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:8]}")


@pytest.fixture
def store(test_settings: Settings) -> Iterator[AtlasStore]:
    store = AtlasStore(test_settings)
    try:
        yield store
    finally:
        # The Atlas user may not dropDatabase; a database with no collections no longer exists.
        for name in store.db.list_collection_names():
            store.db.drop_collection(name)
        store.close()
