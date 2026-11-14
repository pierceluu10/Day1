"""Acceptance 8: `git grep` finds no secrets (working tree, untracked files, every commit) and `.env` is ignored.

Secret values are read from `.env` and the Gitea token file at test run time and never printed.
Cheap and needs nothing running, so it is not gated behind --acceptance.
"""

from __future__ import annotations

import subprocess
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from dayone.config import GITEA_TOKEN_FILE, ROOT

ENV_FILE = ROOT / ".env"
SECRET_NAME_PARTS = ("KEY", "URI", "PASSWORD", "TOKEN", "SECRET")
MIN_SECRET_LENGTH = 8  # shorter values (e.g. "admin") would match ordinary words
NO_MATCH = 1
KEY_PATTERNS = {
    "Anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "Voyage key": r"\bpa-[A-Za-z0-9_-]{30,}",
    "MongoDB URI with credentials": r"mongodb(\+srv)?://[^\s:/@<>${}]+:[^\s@<>${}]+@",
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def secret_values() -> dict[str, str]:
    values = {
        name: value
        for name, value in dotenv_values(ENV_FILE).items()
        if value and any(part in name for part in SECRET_NAME_PARTS)
    }
    if password := urlsplit(values.get("MONGODB_URI", "")).password:
        values["MONGODB_URI password"] = password
    if GITEA_TOKEN_FILE.exists():
        values["Gitea admin token"] = GITEA_TOKEN_FILE.read_text().strip()
    return {name: value for name, value in values.items() if len(value) >= MIN_SECRET_LENGTH}


def grep_files(*pattern_args: str) -> list[str]:
    """Files (rev:path for history) where git grep matches, in the working tree, untracked files and all commits."""
    revisions = git("rev-list", "--all").stdout.split()
    hits = []
    for scope in (["--untracked"], revisions):
        result = git("grep", "-l", "-I", *pattern_args, *scope)
        assert result.returncode in (0, NO_MATCH), result.stderr
        hits += result.stdout.split()
    return hits


def test_env_is_ignored_and_untracked():
    assert git("check-ignore", "-q", ".env").returncode == 0, ".env is not git-ignored"
    assert git("ls-files", "--error-unmatch", ".env").returncode != 0, ".env is tracked"
    assert git("log", "--all", "--format=%h", "--", ".env").stdout == "", ".env was committed at some point"


@pytest.mark.skipif(not ENV_FILE.exists(), reason=".env missing: nothing to compare against")
def test_no_env_secret_value_is_in_git():
    values = secret_values()
    print(f"checking {len(values)} secret values: {sorted(values)}")
    assert values, f"no secret values found in {ENV_FILE}"
    leaks = {name: files for name, value in values.items() if (files := grep_files("-F", "-e", value))}
    assert leaks == {}, f"secret values found in git (names only): {leaks}"


def test_no_common_key_format_is_in_git():
    leaks = {name: files for name, pattern in KEY_PATTERNS.items() if (files := grep_files("-P", "-e", pattern))}
    assert leaks == {}, f"key-shaped strings found in git: {leaks}"
