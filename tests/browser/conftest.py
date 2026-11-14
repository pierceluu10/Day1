"""Live fixtures: the real Gitea (hire linus), a real ubuntu container, real Claude; memory and events are in-test fakes."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import time
from typing import Any

import anthropic
import pytest

from dayone.browser.session import PROFILE_DIR, PlaywrightSession
from dayone.config import STATE_DIR, settings
from dayone.contracts import (
    DOCKER_NETWORK,
    MACHINE_IMAGES,
    ExecResult,
    Hire,
    LocatorHint,
    Machine,
    StepContext,
    container_name,
    idempotency_key,
)
from dayone.gitea import GiteaClient

HIRE = "linus"
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


class ClaudeLLM:
    """Real model, direct SDK call, standing in for dayone.llm.anthropic.AnthropicLLM until it merges."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings().anthropic_api_key, timeout=settings().llm_timeout_s)
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str, max_tokens: int) -> str:
        self.prompts.append(prompt)
        reply = self.client.messages.create(
            model=settings().llm_model, system=system, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in reply.content if block.type == "text")
        print("LLM ->", text)
        return text


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


@pytest.fixture
def clean_linus(gitea, container):
    """linus with no keys, an unticked Welcome issue, no browser profile, and an empty clone dir."""
    for key in gitea.user_keys(HIRE):
        gitea.request("DELETE", f"/admin/users/{HIRE}/keys/{key['id']}")
    issue = gitea.welcome_issue(HIRE)
    gitea.request("PATCH", f"/repos/acme/acme-api/issues/{issue['number']}",
                  json={"body": issue["body"].replace("- [x] ", "- [ ] ")})
    for thread in gitea.request("GET", "/notifications", params={"all": "true", "sudo": HIRE, "limit": 50}):
        if thread["subject"]["html_url"].endswith(f"/issues/{issue['number']}") and not thread["unread"]:
            gitea.request("PATCH", f"/notifications/threads/{thread['id']}", params={"to-status": "unread", "sudo": HIRE})
    shutil.rmtree(PROFILE_DIR / HIRE, ignore_errors=True)
    DockerExec(container).exec("rm -rf /root/acme-api ~/.ssh/known_hosts", 30)


@pytest.fixture
def make_context(gitea, container):
    sessions: list[PlaywrightSession] = []

    def make(step_id: str, memory: DictMemory, events: RecordingEvents, llm: ClaudeLLM | None = None) -> StepContext:
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
