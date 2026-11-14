"""Acceptance 7: every number the API serves matches a direct Atlas query (atlas.numbers / the steps collection).

Deliberately recomputed here, not through dayone.api.queries: the oracle must not share code with what it checks.
"""

from __future__ import annotations

import socket
import time
from datetime import datetime
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from atlas import ledger, numbers
from dayone.contracts import Machine
from harness import Agent, requires, requires_system

pytestmark = [pytest.mark.system, requires_system, requires("dayone.api")]

COMPARE_PATH = "/api/compare/{hire}"
STEPS_PATH = "/api/hires/{hire}/steps"
NUMBER_FIELDS = ("elapsed_s", "errors", "fixes_from_memory", "llm_calls")
ELAPSED_TOLERANCE_S = 1.0  # the API may round elapsed time to whole seconds
SERVER_START_TIMEOUT_S = 30.0
SERVER_POLL_S = 0.5
HTTP_TIMEOUT_S = 10.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def numeric_leaves(value: Any, path: str = "") -> Iterator[tuple[str, float]]:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from numeric_leaves(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from numeric_leaves(item, f"{path}[{index}]")


def dicts_with(value: Any, key: str) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if key in value:
            yield value
        for item in value.values():
            yield from dicts_with(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from dicts_with(item, key)


@pytest.fixture
def api(log_dir, monkeypatch) -> Iterator[httpx.Client]:
    port = free_port()
    monkeypatch.setenv("API_PORT", str(port))
    server = Agent(log_dir, "serve")
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=HTTP_TIMEOUT_S)
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while True:
        assert server.process.poll() is None, f"dayone serve exited:\n{server.output_tail()}"
        try:
            client.get("/")
            break
        except httpx.TransportError:
            assert time.monotonic() < deadline, f"dayone serve not answering on {port}:\n{server.output_tail()}"
            time.sleep(SERVER_POLL_S)
    yield client
    client.close()
    server.kill()


def test_api_numbers_match_atlas(onboard, db, api, hires):
    baseline, current = hires["first"], hires["second"]
    for name in {baseline, current}:
        assert onboard(name, Machine.UBUNTU).exit_code == 0

    response = api.get(COMPARE_PATH.format(hire=current))
    assert response.status_code == 200, response.text
    compare = response.json()
    print(f"GET {COMPARE_PATH.format(hire=current)} -> {compare}")

    entries = {entry["hire"]: entry for entry in dicts_with(compare, "hire")}
    assert {baseline, current} <= set(entries), f"compare does not show both {baseline} and {current}: {sorted(entries)}"
    mismatches = []
    for hire, entry in entries.items():
        expected = numbers(db, hire)
        print(f"Atlas {expected}")
        for field in NUMBER_FIELDS:
            assert field in entry, f"compare entry for {hire} lacks {field}: {entry}"
            want, got = getattr(expected, field), entry[field]
            close = abs(got - want) <= ELAPSED_TOLERANCE_S if field == "elapsed_s" and None not in (got, want) else got == want
            if not close:
                mismatches.append(f"{hire}.{field}: API {got} vs Atlas {want}")
    assert mismatches == []
    uncompared = [path for path, _ in numeric_leaves(compare) if path.rsplit(".", 1)[-1] not in NUMBER_FIELDS]
    assert uncompared == [], f"compare serves numbers this test does not check against Atlas: {uncompared}"

    for hire in (baseline, current):
        response = api.get(STEPS_PATH.format(hire=hire))
        assert response.status_code == 200, response.text
        served = list(dicts_with(response.json(), "step_id"))
        atlas_steps = ledger(db, hire)
        assert sorted(step["step_id"] for step in served) == sorted(atlas_steps)
        for step in served:
            doc = atlas_steps[step["step_id"]]
            assert {"status", "evidence"} <= step.keys(), step
            for key in step.keys():
                assert key in doc or not isinstance(step[key], int | float), f"{hire}/{step['step_id']}.{key} is not an Atlas field"
                if key in doc and isinstance(doc[key], datetime):
                    assert datetime.fromisoformat(step[key]).replace(tzinfo=None) == doc[key], f"{hire}/{step['step_id']}.{key}"
                elif key in doc:
                    assert step[key] == doc[key], f"{hire}/{step['step_id']}.{key}: API {step[key]!r} vs Atlas {doc[key]!r}"
