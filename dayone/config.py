"""Settings from the environment (`.env` at the repo root). No defaults for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".dayone"  # git-ignored; seed writes the Gitea admin token here
GITEA_TOKEN_FILE = STATE_DIR / "gitea_admin_token"


class MissingSetting(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise MissingSetting(f"{name} is not set; copy .env.example to .env and fill it in")
    return value


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str = field(repr=False)
    mongodb_db: str
    anthropic_api_key: str = field(repr=False)
    llm_model: str
    llm_timeout_s: float
    voyage_api_key: str = field(repr=False)
    embedding_base_url: str
    embedding_model: str
    embedding_dimensions: int
    embedding_timeout_s: float
    gitea_url: str  # from the host (browser, API checks)
    gitea_admin_user: str
    gitea_admin_password: str = field(repr=False)
    gitea_hire_password: str = field(repr=False)
    exec_timeout_s: float
    api_port: int
    browser_headless: bool  # false for the demo; true in CI-style tests


@cache
def settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        mongodb_uri=_required("MONGODB_URI"),
        mongodb_db=_required("MONGODB_DB"),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        llm_model=_required("LLM_MODEL"),
        llm_timeout_s=float(_required("LLM_TIMEOUT_SECONDS")),
        voyage_api_key=_required("VOYAGE_API_KEY"),
        embedding_base_url=_required("EMBEDDING_BASE_URL"),
        embedding_model=_required("EMBEDDING_MODEL"),
        embedding_dimensions=int(_required("EMBEDDING_DIMENSIONS")),
        embedding_timeout_s=float(_required("EMBEDDING_TIMEOUT_SECONDS")),
        gitea_url=_required("GITEA_URL"),
        gitea_admin_user=_required("GITEA_ADMIN_USER"),
        gitea_admin_password=_required("GITEA_ADMIN_PASSWORD"),
        gitea_hire_password=_required("GITEA_HIRE_PASSWORD"),
        exec_timeout_s=float(_required("EXEC_TIMEOUT_SECONDS")),
        api_port=int(_required("API_PORT")),
        browser_headless=_required("BROWSER_HEADLESS").lower() == "true",
    )
