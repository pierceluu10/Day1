"""Settings from settings.toml at the repo root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "settings.toml"
EXAMPLE_FILE_NAME = "settings.example.toml"


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str


def load(path: Path) -> Settings:
    if not path.exists():
        raise SettingsError(f"{path.name} not found at {path} (copy {EXAMPLE_FILE_NAME} to {path.name})")
    data = tomllib.loads(path.read_text())
    try:
        return Settings(app_name=data["app"]["name"], database_url=data["database"]["url"])
    except KeyError as error:
        raise SettingsError(f"{path} is missing {error.args[0]!r} (see {EXAMPLE_FILE_NAME})") from error


@cache
def settings() -> Settings:
    return load(SETTINGS_FILE)
