"""The page's backend: serves `web/`, launches engine runs, streams Atlas events, answers every number from Atlas."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dayone.api import processes, queries
from dayone.config import ROOT, settings
from dayone.contracts import HIRES, STEP_IDS, Machine

WEB_DIR = ROOT / "web"
POLL_INTERVAL_S = 0.4
EVENT_BATCH = 500
# A page opening a run gets the step state in full but only this much of the old terminal output.
REPLAY_TERMINAL_LINES = 200
SSE_EVENT = "dayone"  # one Atlas event
SSE_PROCESS = "process"  # the engine process for this hire started or stopped


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client, app.state.db = queries.connect(settings())
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Preflight", lifespan=lifespan)


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _known_hire(hire: str) -> str:
    if hire not in HIRES:
        raise HTTPException(status_code=404, detail=f"unknown hire {hire!r}; hires are {', '.join(HIRES)}")
    return hire


KnownHire = Annotated[str, Depends(_known_hire)]


def _last_event_id(last_event_id: Annotated[str | None, Header()] = None) -> ObjectId | None:
    try:
        return ObjectId(last_event_id) if last_event_id else None
    except InvalidId as error:
        raise HTTPException(status_code=400, detail=f"Last-Event-ID is not an event id: {last_event_id!r}") from error


class RunRequest(BaseModel):
    hire: str
    machine: Machine


@app.post("/api/runs", status_code=202)
def start_run(request: RunRequest) -> dict[str, Any]:
    hire = _known_hire(request.hire)
    if (pid := processes.running_pid(hire)) is not None:
        raise HTTPException(status_code=409, detail=f"{hire} is already running (pid {pid})")
    pid = processes.spawn(hire, ["start", "--hire", hire, "--machine", request.machine.value])
    return {"hire": hire, "pid": pid}


@app.post("/api/runs/{hire}/resume", status_code=202)
def resume_run(hire: KnownHire) -> dict[str, Any]:
    if (pid := processes.running_pid(hire)) is not None:
        raise HTTPException(status_code=409, detail=f"{hire} is already running (pid {pid})")
    return {"hire": hire, "pid": processes.spawn(hire, ["resume", "--hire", hire])}


@app.get("/api/hires")
def list_hires() -> list[dict[str, Any]]:
    summaries = queries.hires(app.state.db)
    return _jsonable([{**s, "running": processes.running_pid(s["hire"]) is not None} for s in summaries])


@app.get("/api/hires/{hire}/steps")
def hire_steps(hire: KnownHire) -> list[dict[str, Any]]:
    return _jsonable(queries.steps(app.state.db, hire))


@app.get("/api/compare/{hire}")
def compare(hire: KnownHire) -> dict[str, Any]:
    return _jsonable(queries.compare(app.state.db, hire))


@app.get("/api/config")
def config() -> dict[str, list[str]]:
    return {"steps": list(STEP_IDS), "hires": list(HIRES)}


@app.get("/api/hires/{hire}/events", response_class=EventSourceResponse)
async def hire_events(
    hire: KnownHire, after: Annotated[ObjectId | None, Depends(_last_event_id)]
) -> AsyncIterator[ServerSentEvent]:
    running: bool | None = None
    if after is None:
        for doc in await asyncio.to_thread(queries.replay, app.state.db, hire, REPLAY_TERMINAL_LINES):
            after = doc["_id"]
            yield ServerSentEvent(id=str(after), event=SSE_EVENT, data=_jsonable(doc))
    while True:
        batch = await asyncio.to_thread(queries.events_after, app.state.db, hire, after, EVENT_BATCH)
        for doc in batch:
            after = doc["_id"]
            yield ServerSentEvent(id=str(after), event=SSE_EVENT, data=_jsonable(doc))
        now_running = processes.running_pid(hire) is not None
        if now_running != running:
            running = now_running
            yield ServerSentEvent(event=SSE_PROCESS, data={"running": running})
        if len(batch) < EVENT_BATCH:
            await asyncio.sleep(POLL_INTERVAL_S)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
