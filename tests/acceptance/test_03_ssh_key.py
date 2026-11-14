"""Acceptance 3: the SSH key step is verified only when the Gitea API lists the key AND `ssh -T` succeeds.

After a real run, QA confirms both routes itself, then breaks each route in turn (key removed from Gitea
through the admin API; private key moved aside in the container) and calls the step's real check:
it must fail each time, and pass again once restored.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from atlas import ledger
from dayone.config import settings
from dayone.contracts import GITEA_HOSTNAME, DONE_STATUSES, Machine, StepContext, idempotency_key
from harness import container_exec, requires_system

pytestmark = [pytest.mark.system, requires_system]

STEP_ID = "b_add_ssh_key"
SSH_AUTHENTICATED = "successfully authenticated"
SSH_TEST = f"ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new git@{GITEA_HOSTNAME}"
PUBLIC_KEYS = "for key in /root/.ssh/*.pub; do echo \"$key $(ssh-keygen -lf \"$key\")\"; done"
MOVED_SUFFIX = ".qa-moved"


def container_keys(hire: str) -> dict[str, str]:
    """Public key path -> SHA256 fingerprint, read with ssh-keygen in the hire's container."""
    result = container_exec(hire, PUBLIC_KEYS)
    assert result.returncode == 0, result.stdout + result.stderr
    keys = {}
    for line in result.stdout.splitlines():
        path, _bits, fingerprint, *_ = line.split()
        keys[path] = fingerprint
    return keys


def ssh_test(hire: str) -> str:
    result = container_exec(hire, SSH_TEST)
    return result.stdout + result.stderr


@pytest.fixture
def context(hires) -> Iterator[StepContext]:
    """A StepContext wired from the real components, exactly as the engine wires them."""
    from dayone.browser.session import PlaywrightSession
    from dayone.gitea import GiteaClient
    from dayone.llm.anthropic import AnthropicLLM
    from dayone.llm.voyage import VoyageEmbedder
    from dayone.memory.experience import ExperienceMemory
    from dayone.store.atlas import AtlasStore
    from dayone.store.events import AtlasEventBus
    from dayone.terminal.docker import DockerExecutor

    config = settings()
    store = AtlasStore(config)
    name = hires["first"]
    hire = store.get_hire(name)
    assert hire is not None, f"no {name} in hires"
    events = AtlasEventBus(store.db, hire)
    gitea = GiteaClient(config)
    memory = ExperienceMemory(store.db, AnthropicLLM(config), VoyageEmbedder(config), events, gitea, config)
    browser = PlaywrightSession(hire, config, events, memory)
    yield StepContext(
        hire=hire,
        step_id=STEP_ID,
        idempotency_key=idempotency_key(name, STEP_ID),
        executor=DockerExecutor(hire, events, config.exec_timeout_s),
        browser=browser,
        gitea=gitea,
        events=events,
        memory=memory,
    )
    browser.close()
    gitea.close()


@contextmanager
def key_removed_from_gitea(gitea, hire: str, fingerprint: str) -> Iterator[None]:
    key = next(key for key in gitea.user_keys(hire) if key["fingerprint"] == fingerprint)
    gitea.request("DELETE", f"/admin/users/{hire}/keys/{key['id']}")
    print(f"removed Gitea key {key['id']} {key['title']!r}")
    try:
        yield
    finally:
        gitea.request("POST", f"/admin/users/{hire}/keys", json={"title": key["title"], "key": key["key"]})
        print(f"restored Gitea key {key['title']!r}")


@contextmanager
def private_key_moved(hire: str, public_path: str) -> Iterator[None]:
    private = public_path.removesuffix(".pub")
    moved = container_exec(hire, f"mv {private} {private}{MOVED_SUFFIX}")
    assert moved.returncode == 0, moved.stderr
    try:
        yield
    finally:
        restored = container_exec(hire, f"mv {private}{MOVED_SUFFIX} {private}")
        assert restored.returncode == 0, restored.stderr


def test_ssh_key_needs_both_api_and_ssh(onboard, db, gitea, context, hires):
    hire = hires["first"]
    run = onboard(hire, Machine.UBUNTU)
    assert run.exit_code == 0, run.output_tail
    step = ledger(db, hire)[STEP_ID]
    assert step["status"] in DONE_STATUSES, step

    from dayone.steps.browser_steps import BROWSER_STEPS

    check = BROWSER_STEPS[STEP_ID].check
    api_fingerprints = {key["fingerprint"] for key in gitea.user_keys(hire)}
    listed = {path: fingerprint for path, fingerprint in container_keys(hire).items() if fingerprint in api_fingerprints}
    print(f"container keys listed by the API: {listed}; ledger evidence: {step['evidence']}")
    assert listed, "no public key in the container is listed by the Gitea API"
    public_path, fingerprint = next(iter(listed.items()))
    assert fingerprint in step["evidence"], f"evidence does not name the key the API lists: {step['evidence']}"
    assert SSH_AUTHENTICATED in ssh_test(hire)

    baseline = check(context)
    assert baseline.ok, baseline

    with key_removed_from_gitea(gitea, hire, fingerprint):
        without_api = check(context)
        print(f"check with the key missing from Gitea: {without_api}")
        assert not without_api.ok

    with private_key_moved(hire, public_path):
        assert SSH_AUTHENTICATED not in ssh_test(hire), "ssh -T still authenticates: breaking it did not work"
        assert fingerprint in {key["fingerprint"] for key in gitea.user_keys(hire)}
        without_ssh = check(context)
        print(f"check with ssh -T failing but the API listing the key: {without_ssh}")
        assert not without_ssh.ok

    restored = check(context)
    assert restored.ok, f"check fails after both routes were restored: {restored}"
