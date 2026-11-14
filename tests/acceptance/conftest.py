"""Session fixtures: an emptied `dayone_acceptance` database, the Gitea API, and real onboarding runs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pymongo import MongoClient
from pymongo.database import Database

from atlas import latest_run_id
from dayone.config import GITEA_TOKEN_FILE, settings
from dayone.contracts import REPO, Machine
from harness import Agent, container_exists, run_cli

TICKED_ITEM = "- [x]"
DEFAULT_HIRES = {"first": "ada", "second": "grace", "killed": "barbara", "alpine": "ken", "drift": "linus"}


@dataclass(frozen=True)
class Run:
    hire: str
    machine: Machine
    exit_code: int
    output_tail: str
    run_id: str


@pytest.fixture(scope="session")
def hires(request: pytest.FixtureRequest) -> dict[str, str]:
    """Role -> Gitea hire. `--hire NAME` maps every role to NAME, for when other hires are in use."""
    only = request.config.getoption("--hire")
    return dict.fromkeys(DEFAULT_HIRES, only) if only else DEFAULT_HIRES


@pytest.fixture(scope="session")
def db(acceptance_db_name: str) -> Iterator[Database]:
    configured = settings().mongodb_db
    assert configured == acceptance_db_name, f"refusing to empty {configured!r}; run with --acceptance"
    client: MongoClient = MongoClient(settings().mongodb_uri)
    db = client[acceptance_db_name]
    # The hackathon user may not dropDatabase; emptying collections also keeps the vector index built.
    for name in db.list_collection_names():
        print(f"emptied {acceptance_db_name}.{name}: {db[name].delete_many({}).deleted_count} documents")
    yield db
    client.close()


@pytest.fixture(scope="session")
def gitea():
    from dayone.gitea import GiteaClient

    assert GITEA_TOKEN_FILE.exists(), f"{GITEA_TOKEN_FILE} missing: run scripts/up.sh"
    client = GiteaClient(settings())
    yield client
    client.close()


@pytest.fixture(scope="session")
def log_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("dayone_logs")


@pytest.fixture(scope="session")
def fresh_hire(gitea) -> Callable[[str], None]:
    """`dayone reset NAME`, then prove it left the hire with nothing: no container, no Gitea keys, no ticks."""

    def reset(hire: str) -> None:
        run_cli("reset", hire)
        assert not container_exists(hire), f"dayone reset {hire} left container dayone-{hire} behind"
        keys = gitea.user_keys(hire)
        assert keys == [], f"dayone reset {hire} left Gitea SSH keys: {[key['title'] for key in keys]}"
        body = gitea.welcome_issue(hire)["body"]
        assert TICKED_ITEM not in body, f"{hire}'s Welcome issue on {REPO} is still ticked:\n{body}"

    return reset


@pytest.fixture(scope="session")
def onboard(db: Database, log_dir: Path, fresh_hire) -> Callable[[str, Machine], Run]:
    """Runs `dayone start` for a hire once per session from a fresh state; later calls return the same run."""
    runs: dict[str, Run] = {}

    def run(hire: str, machine: Machine) -> Run:
        if hire not in runs:
            fresh_hire(hire)
            agent = Agent(log_dir, "start", "--hire", hire, "--machine", machine.value)
            code = agent.wait()
            runs[hire] = Run(hire, machine, code, agent.output_tail(), latest_run_id(db, hire))
        assert runs[hire].machine == machine, f"{hire} already ran on {runs[hire].machine} this session"
        return runs[hire]

    return run
