"""Real Atlas, Voyage and Claude in a throwaway database. Only Gitea and the event bus are faked."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

import pytest
from pymongo import MongoClient

from dayone.config import settings
from dayone.contracts import EventType
from dayone.llm.anthropic import AnthropicLLM
from dayone.llm.voyage import VoyageEmbedder
from dayone.memory.experience import EXPERIENCES, VECTOR_INDEX, ExperienceMemory

SEARCH_VISIBLE_WAIT_S = 60.0
SEARCH_POLL_S = 1.0


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, str | None, dict[str, Any]]] = []

    def emit(self, type: EventType, step_id: str | None = None, **data: Any) -> None:
        print(f"event {type} {step_id} {data}")
        self.events.append((type, step_id, data))

    def of(self, type: EventType) -> list[dict[str, Any]]:
        return [data for event_type, _, data in self.events if event_type == type]

    def tail(self, hire: str, after: datetime | None):
        raise NotImplementedError("memory never tails events")


class RecordingLLM:
    """The real model, with every prompt and reply kept so tests can count calls and read the feedback loop."""

    def __init__(self, llm: AnthropicLLM) -> None:
        self._llm = llm
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        self.prompts.append(prompt)
        reply = self._llm.complete(system, prompt, max_tokens)
        print(f"--- LLM prompt {len(self.prompts)} ---\n{prompt}\n--- reply ---\n{reply}")
        return reply


class FakeGitea:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}

    def find_issue(self, repo: str, title: str) -> dict[str, Any] | None:
        return self.issues.get(f"{repo}:{title}")

    def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        number = len(self.issues) + 1
        issue = {"number": number, "title": title, "body": body, "html_url": f"http://gitea.test/{repo}/issues/{number}"}
        self.issues[f"{repo}:{title}"] = issue
        return issue


@pytest.fixture(scope="session")
def database():
    client = MongoClient(settings().mongodb_uri)
    db = client[f"dayone_test_memory_{uuid.uuid4().hex[:8]}"]
    yield db
    db.drop_collection(EXPERIENCES)  # the Atlas user may not dropDatabase; an empty database disappears
    client.close()


@pytest.fixture(scope="session")
def embedder():
    embedder = VoyageEmbedder(settings())
    yield embedder
    embedder.close()


@pytest.fixture(scope="session")
def indexed(database, embedder):
    memory = ExperienceMemory(database, AnthropicLLM(settings()), embedder, RecordingEvents(), FakeGitea(), settings())
    started = time.monotonic()
    memory.ensure_indexes()
    print(f"vector index queryable after {time.monotonic() - started:.1f}s")
    return database


@pytest.fixture
def harness(indexed, embedder):
    indexed[EXPERIENCES].delete_many({})
    llm = RecordingLLM(AnthropicLLM(settings()))
    events = RecordingEvents()
    gitea = FakeGitea()
    memory = ExperienceMemory(indexed, llm, embedder, events, gitea, settings())
    return memory, llm, events, gitea, indexed[EXPERIENCES]


def wait_until_searchable(collection, count: int) -> None:
    """Atlas Search indexes new documents asynchronously; wait until `count` verified ones are visible."""
    deadline = time.monotonic() + SEARCH_VISIBLE_WAIT_S
    probe = [0.0] * settings().embedding_dimensions
    probe[0] = 1.0
    while True:
        visible = list(
            collection.aggregate(
                [
                    {
                        "$vectorSearch": {
                            "index": VECTOR_INDEX,
                            "path": "embedding",
                            "queryVector": probe,
                            "numCandidates": 100,
                            "limit": 100,
                            "filter": {"outcome": "verified"},
                        }
                    },
                    {"$project": {"_id": 1}},
                ]
            )
        )
        if len(visible) >= count:
            return
        assert time.monotonic() < deadline, f"only {len(visible)}/{count} experiences searchable"
        time.sleep(SEARCH_POLL_S)
