"""Live fixtures: the real Gitea (dev user dev4, seeded like a hire), a real ubuntu container, real Claude, and
either in-test fake memory and events or, through AtlasRunner, the real Atlas memory and event bus (dev DB)."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import time
import uuid
from typing import Any

import pytest
from pymongo import MongoClient
from pymongo.database import Database

from dayone.browser.session import PROFILE_DIR, PlaywrightSession
from dayone.config import STATE_DIR, settings
from dayone.contracts import (
    CheckResult,
    EventType,
    DOCKER_NETWORK,
    MACHINE_IMAGES,
    REPO_DIR,
    ExecResult,
    Hire,
    LocatorHint,
    Machine,
    StepContext,
    container_name,
    idempotency_key,
)
from dayone.gitea import GiteaClient
from dayone.llm.anthropic import AnthropicLLM
from dayone.llm.voyage import VoyageEmbedder
from dayone.memory.experience import ExperienceMemory
from dayone.steps.browser_steps import BROWSER_STEPS
from dayone.store.events import AtlasEventBus
from scripts.seed import TEAM, ensure_hire, ensure_team

HIRE = "dev4"  # session 4's own Gitea user and container; the five demo hires are never touched
SCREENSHOT_DIR = STATE_DIR / "screenshots"
CONTAINER_SETUP = (
    "apt-get update -qq && apt-get install -y -qq openssh-client git >/dev/null"
    f" && (test -f ~/.ssh/id_ed25519 || ssh-keygen -q -t ed25519 -N '' -C {idempotency_key(HIRE, 't_ssh_keygen')}"
    " -f ~/.ssh/id_ed25519)"
)


class DockerExec:
    """Just enough of `Executor` for browser steps: `docker exec ... sh -lc`."""

    def __init__(self, container: str):
        self.container = container

    def exec(self, command: str, timeout_s: float, workdir: str | None = None) -> ExecResult:
        args = ["docker", "exec", *(["-w", workdir] if workdir else []), self.container, "sh", "-lc", command]
        started = time.monotonic()
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)
        print(f"$ {command}\n{done.stdout}{done.stderr}")
        return ExecResult(command, done.returncode, done.stdout + done.stderr, time.monotonic() - started)


class RecordingEvents:
    def __init__(self):
        self.events: list[tuple[str, str | None, dict[str, Any]]] = []

    def emit(self, type, step_id=None, **data):
        print("event", type, step_id, data)
        self.events.append((type, step_id, data))

    def modes(self, goal: str | None = None) -> list[str]:
        return [d["mode"] for t, _, d in self.events if t == "locator" and (goal is None or d["goal"] == goal)]

    def tail(self, hire, after):
        return iter(())


class DictMemory:
    """Exact-goal locator memory; the real one (session 5) adds vector search over goal + page summary."""

    def __init__(self):
        self.hints: dict[str, LocatorHint] = {}
        self.saved: list[tuple[LocatorHint, str]] = []

    def find_locator(self, goal: str, page_summary: str) -> LocatorHint | None:
        return self.hints.get(goal)

    def save_locator(self, hint: LocatorHint, hire: str) -> None:
        self.saved.append((hint, hire))
        self.hints[hint.goal] = dataclasses.replace(hint, source_hire=hire)


class CountingLLM(AnthropicLLM):
    """The real model, recording every prompt so tests can count reasoning calls."""

    def __init__(self):
        super().__init__(settings())
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        self.prompts.append(prompt)
        reply = super().complete(system, prompt, max_tokens)
        print("LLM ->", reply)
        return reply


def reconcile(step_id: str, ctx: StepContext) -> tuple[CheckResult, CheckResult]:
    """What the engine does: check, run, check. Returns (check before, check after)."""
    step = BROWSER_STEPS[step_id]
    before = step.check(ctx)
    print(step_id, "before:", before)
    assert not before.ok, f"{step_id} check passed before run: it cannot tell done from not done"
    run = step.run(ctx)
    print(step_id, "run:", run)
    after = step.check(ctx)
    print(step_id, "after:", after)
    return before, after


def reset_dev_hire(gitea: GiteaClient, container: str) -> None:
    """The dev hire with no keys, an unticked unread Welcome issue, no browser profile, and no clone."""
    gitea.reset_hire(HIRE)
    shutil.rmtree(PROFILE_DIR / HIRE, ignore_errors=True)
    DockerExec(container).exec(f"rm -rf {REPO_DIR} ~/.ssh/known_hosts", 30)


def locator_events(db: Database, run_id: str, goal: str) -> list[dict[str, Any]]:
    query = {"hire": HIRE, "run_id": run_id, "type": EventType.LOCATOR.value, "data.goal": goal}
    return [event["data"] for event in db["events"].find(query).sort([("ts", 1), ("seq", 1)])]


@pytest.fixture(scope="session")
def gitea():
    client = GiteaClient(settings())
    yield client
    client.close()


@pytest.fixture(scope="session")
def container() -> str:
    name = container_name(HIRE)
    running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True)
    if running.stdout.strip() != "true":
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", DOCKER_NETWORK,
             MACHINE_IMAGES[Machine.UBUNTU], "tail", "-f", "/dev/null"],
            check=True, capture_output=True,
        )
    result = DockerExec(name).exec(CONTAINER_SETUP, 300)
    assert result.ok, result.output
    return name


@pytest.fixture(scope="session")
def dev_hire(gitea) -> str:
    """A Gitea user seeded exactly like a hire (team member, assigned Welcome issue) but never used by the demo."""
    ensure_hire(gitea, ensure_team(gitea, TEAM, "write"), HIRE, settings().gitea_hire_password)
    return HIRE


@pytest.fixture
def clean_hire(dev_hire, gitea, container):
    reset_dev_hire(gitea, container)


@pytest.fixture
def make_context(gitea, container):
    sessions: list[PlaywrightSession] = []

    def make(step_id: str, memory: DictMemory, events: RecordingEvents, llm: CountingLLM | None = None) -> StepContext:
        hire = Hire(HIRE, Machine.UBUNTU, container, "test-run")
        browser = PlaywrightSession(hire, dataclasses.replace(settings(), browser_headless=True), events, memory, llm)
        sessions.append(browser)
        return StepContext(hire, step_id, idempotency_key(HIRE, step_id), DockerExec(container), browser,
                           gitea, events, memory)

    yield make
    for browser in sessions:
        browser.close()


@pytest.fixture
def screenshots(request):
    """Wraps a session's overlay calls so every ring is captured on the real page."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    taken: list[str] = []

    def install(browser: PlaywrightSession) -> list[str]:
        for name in ("_mark", "_mark_moved"):
            original = getattr(browser, name)

            def spy(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                mode = args[1] if len(args) > 1 else "moved"
                path = SCREENSHOT_DIR / f"{request.node.name}-{len(taken):02d}-{mode}.png"
                browser.page.screenshot(path=str(path))
                taken.append(str(path))
                return result

            setattr(browser, name, spy)
        return taken

    return install


@pytest.fixture(scope="session")
def db():
    client = MongoClient(settings().mongodb_uri, tz_aware=True)  # as AtlasStore does; AtlasEventBus compares aware ts
    assert settings().mongodb_db.startswith("dayone_dev_"), "these tests write locators; never run them on the demo DB"
    yield client[settings().mongodb_db]
    client.close()


@pytest.fixture(scope="session")
def atlas_memory(db, gitea):
    memory = ExperienceMemory(db, AnthropicLLM(settings()), VoyageEmbedder(settings()), RecordingEvents(), gitea, settings())
    memory.ensure_indexes()
    return memory


class AtlasRunner:
    """Runs b_sign_in + b_add_ssh_key as one hire run on the real stack: Atlas memory and events, real Claude."""

    def __init__(self, db, memory, gitea, container, screenshots):
        self.db, self.memory, self.gitea, self.container = db, memory, gitea, container
        self.install_screenshots = screenshots
        self.llm = CountingLLM()
        self.shots: list[str] = []
        self.durations_s: list[float] = []

    def run(self, headless: bool = True, init_script: str | None = None) -> str:
        run_id = f"dev-{uuid.uuid4().hex[:8]}"
        hire = Hire(HIRE, Machine.UBUNTU, self.container, run_id)
        events = AtlasEventBus(self.db, hire)
        browser = PlaywrightSession(hire, dataclasses.replace(settings(), browser_headless=headless), events, self.memory, self.llm)
        started = time.monotonic()
        try:
            if init_script:
                browser.page.context.add_init_script(init_script)
            self.shots = self.install_screenshots(browser)  # one list shared by every browser in the test
            ctx = StepContext(hire, "", "", DockerExec(self.container), browser, self.gitea, events, self.memory)
            for step_id in ("b_sign_in", "b_add_ssh_key"):
                ctx.step_id, ctx.idempotency_key = step_id, idempotency_key(HIRE, step_id)
                _, after = reconcile(step_id, ctx)
                assert after.ok, after
        finally:
            browser.close()
            events.close()
        self.durations_s.append(time.monotonic() - started)
        return run_id


@pytest.fixture
def atlas_runner(db, atlas_memory, gitea, container, screenshots):
    return AtlasRunner(db, atlas_memory, gitea, container, screenshots)
