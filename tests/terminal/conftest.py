import shutil
import subprocess

import httpx
import pytest

from dayone.config import settings
from dayone.contracts import HIRES, EventType, Hire, Machine, StepContext, container_name, idempotency_key
from dayone.terminal.docker import DockerExecutor

DEV_HIRE = "dev3"  # Gitea user seeded for session 3 only; never a hire, so live tests share nothing with integration
# The laptop is never a hire container: those belong to integration runs (concurrent apt hits the dpkg lock).
DEV_CONTAINER = "dayone-dev3"
HIRE_CONTAINERS = frozenset(container_name(hire) for hire in HIRES)
DOCKER_TIMEOUT_S = 60.0


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, str | None, dict]] = []

    def emit(self, type: EventType, step_id: str | None = None, **data) -> None:
        self.events.append((type, step_id, data))

    def tail(self, hire, after):
        raise NotImplementedError("tests only record")


def docker_available() -> bool:
    return shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def gitea_available() -> bool:
    try:
        return httpx.get(f"{settings().gitea_url.rstrip('/')}/api/healthz", timeout=3).is_success
    except httpx.TransportError:
        return False


@pytest.fixture
def docker() -> None:
    if not docker_available():
        pytest.skip("docker daemon not reachable")


@pytest.fixture
def gitea(docker):
    if not gitea_available():
        pytest.skip("Gitea not running on GITEA_URL")
    from dayone.gitea import GiteaClient

    client = GiteaClient(settings())
    yield client
    client.close()


@pytest.fixture
def dev_hire() -> str:
    return DEV_HIRE


@pytest.fixture
def make_executor(docker):
    def make(machine: Machine, container: str = DEV_CONTAINER) -> DockerExecutor:
        assert container not in HIRE_CONTAINERS, f"tests must not touch hire container {container}"
        return DockerExecutor(Hire(DEV_HIRE, machine, container, "test-run"), RecordingBus(), DOCKER_TIMEOUT_S)

    return make


@pytest.fixture
def make_context():
    def make(executor: DockerExecutor, gitea, step_id: str) -> StepContext:
        # Browser and memory are not used by terminal steps.
        key = idempotency_key(DEV_HIRE, step_id)
        return StepContext(executor.hire, step_id, key, executor, None, gitea, executor.events, None)

    return make
