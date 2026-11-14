"""Builds the concrete parts named in STATUS.md. Kept apart from the loop so the loop is tested with fakes."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager

from dayone.browser.session import PROFILE_DIR, PlaywrightSession
from dayone.config import Settings
from dayone.contracts import Hire
from dayone.engine.runner import Parts, ResetParts
from dayone.gitea import GiteaClient
from dayone.llm.anthropic import AnthropicLLM
from dayone.llm.voyage import VoyageEmbedder
from dayone.memory.experience import ExperienceMemory
from dayone.steps import STEPS
from dayone.store.atlas import AtlasStore
from dayone.store.events import AtlasEventBus
from dayone.terminal.docker import DockerExecutor
from dayone.terminal.fingerprint import fingerprint


@contextmanager
def open_store(settings: Settings) -> Iterator[AtlasStore]:
    store = AtlasStore(settings)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def open_parts(settings: Settings, store: AtlasStore, hire: Hire) -> Iterator[Parts]:
    events = AtlasEventBus(store.db, hire)
    try:
        gitea = GiteaClient(settings)
        memory = ExperienceMemory(store.db, AnthropicLLM(settings), VoyageEmbedder(settings), events, gitea, settings)
        memory.ensure_indexes()
        browser = PlaywrightSession(hire, settings, events, memory)
        try:
            yield Parts(
                events=events,
                executor=DockerExecutor(hire, events, settings.exec_timeout_s),
                browser=browser,
                gitea=gitea,
                memory=memory,
                steps=STEPS,
                fingerprint=fingerprint,
                exec_timeout_s=settings.exec_timeout_s,
            )
        finally:
            browser.close()
    finally:
        events.close()


def reset_parts(settings: Settings, store: AtlasStore) -> ResetParts:
    def remove_container(hire: Hire) -> None:
        events = AtlasEventBus(store.db, hire)
        try:
            DockerExecutor(hire, events, settings.exec_timeout_s).remove_container()
        finally:
            events.close()

    def remove_browser_profile(name: str) -> None:
        profile = PROFILE_DIR / name
        if profile.exists():
            shutil.rmtree(profile)

    return ResetParts(
        reset_gitea=GiteaClient(settings).reset_hire,
        remove_container=remove_container,
        remove_browser_profile=remove_browser_profile,
    )
