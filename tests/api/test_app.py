"""HTTP surface: launching runs, validation at the boundary, and the resumable event stream."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dayone.api import app as app_module
from dayone.api import processes
from dayone.contracts import STEP_IDS

LOCAL_CLIENT = ("127.0.0.1", 50000)
PHONE_CLIENT = ("192.168.7.60", 50000)


@pytest.fixture
def client(db):
    with TestClient(app_module.app, client=LOCAL_CLIENT) as client:
        app_module.app.state.db = db  # the lifespan connected to the dev database; tests use their own
        yield client


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(processes, "running_pid", lambda hire: None)
    monkeypatch.setattr(processes, "spawn", lambda hire, args: calls.append((hire, args)) or 4242)
    return calls


def read_sse(client, hire, last_event_id=None, events_wanted=1):
    """Reads frames until `events_wanted` Atlas events and the first process frame arrived, then disconnects."""
    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    frames, current = [], {}
    with client.stream("GET", f"/api/hires/{hire}/events", headers=headers) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line:
                field, _, value = line.partition(": ")
                current[field] = value
                continue
            if current.get("event"):
                frames.append(current)
            current = {}
            kinds = [frame["event"] for frame in frames]
            if kinds.count(app_module.SSE_EVENT) >= events_wanted and app_module.SSE_PROCESS in kinds:
                break
    return frames


def test_start_spawns_the_cli_for_a_known_hire(client, spawned):
    response = client.post("/api/runs", json={"hire": "barbara", "machine": "ubuntu"})

    assert response.status_code == 202, response.text
    assert response.json() == {"hire": "barbara", "pid": 4242}
    assert spawned == [("barbara", ["start", "--hire", "barbara", "--machine", "ubuntu"])]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"hire": "mallory", "machine": "ubuntu"}, 404),
        ({"hire": "../etc", "machine": "ubuntu"}, 404),
        ({"hire": "barbara", "machine": "windows"}, 422),
    ],
)
def test_start_rejects_unknown_hires_and_machines(client, spawned, body, status):
    assert client.post("/api/runs", json=body).status_code == status
    assert spawned == []


def test_start_refuses_while_the_hire_is_running(client, monkeypatch):
    monkeypatch.setattr(processes, "running_pid", lambda hire: 999)

    response = client.post("/api/runs", json={"hire": "barbara", "machine": "ubuntu"})

    assert response.status_code == 409
    assert "999" in response.json()["detail"]


def test_resume_spawns_resume_for_that_hire(client, spawned):
    assert client.post("/api/runs/ken/resume").status_code == 202
    assert spawned == [("ken", ["resume", "--hire", "ken"])]


def test_steps_and_config(client):
    assert [s["step_id"] for s in client.get("/api/hires/ada/steps").json()] == list(STEP_IDS)
    assert client.get("/api/config").json()["steps"] == list(STEP_IDS)
    assert client.get("/api/hires/nobody/steps").status_code == 404


def test_compare_serialises_dates(client, db, event):
    db.events.insert_many([event("ada", "a", "run_started", 0), event("ada", "a", "run_finished", 61)])

    body = client.get("/api/compare/ada").json()

    assert body["first"]["elapsed_s"] == 61.0
    assert body["first"]["finished_at"].startswith("2026-11-14T09:01:01")


def test_event_stream_resumes_after_last_event_id(client, db, event, monkeypatch):
    monkeypatch.setattr(processes, "running_pid", lambda hire: None)
    db.events.insert_many([event("barbara", "b", "terminal_output", 1, "t_clone", chunk=str(i)) for i in range(3)])

    first = read_sse(client, "barbara", events_wanted=3)
    events = [frame for frame in first if frame["event"] == app_module.SSE_EVENT]
    print(first)
    assert [json.loads(frame["data"])["data"]["chunk"] for frame in events] == ["0", "1", "2"]
    process = [json.loads(frame["data"]) for frame in first if frame["event"] == app_module.SSE_PROCESS]
    assert process == [{"running": False}]

    db.events.insert_one(event("barbara", "b", "terminal_output", 2, "t_clone", chunk="3"))
    resumed = read_sse(client, "barbara", last_event_id=events[0]["id"], events_wanted=3)
    chunks = [json.loads(frame["data"])["data"]["chunk"] for frame in resumed if frame["event"] == app_module.SSE_EVENT]
    assert chunks == ["1", "2", "3"]


def test_event_stream_rejects_a_malformed_last_event_id(client):
    response = client.get("/api/hires/barbara/events", headers={"Last-Event-ID": "not-an-id"})
    assert response.status_code == 400


def test_other_machines_can_watch_but_not_start(db, spawned):
    with TestClient(app_module.app, client=PHONE_CLIENT) as phone:
        app_module.app.state.db = db
        started = phone.post("/api/runs", json={"hire": "barbara", "machine": "ubuntu"})
        resumed = phone.post("/api/runs/barbara/resume")
        config = phone.get("/api/config")
        page = phone.get("/terminal.html")
    print(started.status_code, started.text, resumed.status_code, config.status_code, page.status_code)
    assert started.status_code == resumed.status_code == 403
    assert spawned == []
    assert config.status_code == page.status_code == 200
