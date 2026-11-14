"""Acceptance tests (marked `system`) drive the real Gitea, Docker and Atlas, and run only with --acceptance."""

import os

import pytest

ACCEPTANCE_DB = "dayone_acceptance"
ACCEPTANCE_OPTION = "--acceptance"
HIRE_OPTION = "--hire"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        ACCEPTANCE_OPTION,
        action="store_true",
        help=f"run tests/acceptance against the real system (minutes per test; empties memory in Atlas db {ACCEPTANCE_DB})",
    )
    parser.addoption(
        HIRE_OPTION,
        help="run every acceptance role as this one hire (tests that need two different hires skip)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", f"system: drives the real Day One system; runs only with {ACCEPTANCE_OPTION}")
    if config.getoption(ACCEPTANCE_OPTION):
        # Before anything calls dayone.config.settings() (cached); `dayone` subprocesses inherit it,
        # and load_dotenv never overrides a variable that is already set.
        os.environ["MONGODB_DB"] = ACCEPTANCE_DB


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption(ACCEPTANCE_OPTION):
        return
    skip = pytest.mark.skip(reason=f"real-system acceptance test; run with `pytest {ACCEPTANCE_OPTION}`")
    for item in items:
        if "system" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def acceptance_db_name() -> str:
    return ACCEPTANCE_DB
