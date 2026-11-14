import pytest
from fastapi.testclient import TestClient

from acme_api import db
from acme_api.app import app


@pytest.fixture
def client():
    with db.connection() as conn:
        db.execute(conn, "DELETE FROM users")
    with TestClient(app) as test_client:
        yield test_client
