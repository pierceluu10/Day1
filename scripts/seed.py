"""Seed Gitea: org, team, repo pushed from demo_repo/, hires, Welcome issues. Idempotent.

`--reset` first deletes the hires (with their keys) and the repo (with its issues), then seeds afresh.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from dayone.config import ROOT, settings
from dayone.contracts import HIRES, REPO, REPO_BRANCH, WELCOME_ISSUE_TITLE
from dayone.gitea import GiteaClient, GiteaError, GiteaNotFound, read_admin_token, welcome_body

ORG = REPO.split("/")[0]
TEAM = "engineering"
TEAM_UNITS = ("repo.code", "repo.issues", "repo.pulls", "repo.releases", "repo.wiki")
HIRE_EMAIL = "{hire}@acme.test"
DEMO_REPO = ROOT / "demo_repo"
DEMO_REPO_IGNORED = (".venv", "__pycache__", "*.egg-info", ".pytest_cache", "settings.toml", "*.db")
COMMIT_AUTHOR = ("Acme Engineering", "engineering@acme.test")
INITIAL_COMMIT_MESSAGE = "acme-api 1.4.0"
# Fixed dates make the seeded commit's SHA the same after every reset (same demo_repo/ content).
COMMIT_DATE = "2026-11-02T09:00:00+00:00"
FIRST_PUSH_ATTEMPTS = 20
FIRST_PUSH_POLL_S = 0.5


def exists(gitea: GiteaClient, path: str) -> bool:
    try:
        gitea.request("GET", path)
    except GiteaNotFound:
        return False
    return True


def ensure_org(gitea: GiteaClient) -> None:
    if not exists(gitea, f"/orgs/{ORG}"):
        gitea.request("POST", "/orgs", json={"username": ORG, "visibility": "public"})
        print(f"created org {ORG}")


def ensure_team(gitea: GiteaClient) -> int:
    for team in gitea.request("GET", f"/orgs/{ORG}/teams"):
        if team["name"] == TEAM:
            return team["id"]
    team = gitea.request(
        "POST",
        f"/orgs/{ORG}/teams",
        json={
            "name": TEAM,
            "permission": "write",
            "includes_all_repositories": True,
            "units": list(TEAM_UNITS),
            "units_map": {unit: "write" for unit in TEAM_UNITS},
        },
    )
    print(f"created team {TEAM} (write on all {ORG} repos)")
    return team["id"]


def git(*args: str, cwd: Path) -> None:
    env = {**os.environ, "GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


def push_demo_repo(gitea_url: str, repo: str) -> None:
    name, email = COMMIT_AUTHOR
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / repo.split("/")[1]
        shutil.copytree(DEMO_REPO, work, ignore=shutil.ignore_patterns(*DEMO_REPO_IGNORED))
        git("init", "-b", REPO_BRANCH, cwd=work)
        git("add", ".", cwd=work)
        git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", INITIAL_COMMIT_MESSAGE, cwd=work)
        git(
            "-c",
            f"http.extraHeader=Authorization: token {read_admin_token()}",
            "push",
            f"{gitea_url.rstrip('/')}/{repo}.git",
            f"{REPO_BRANCH}:{REPO_BRANCH}",
            cwd=work,
        )


def ensure_repo(gitea: GiteaClient, gitea_url: str, repo: str) -> None:
    """Creates `repo` (owner/name, owner = an org) and pushes demo_repo/ to it unless its branch exists."""
    owner, name = repo.split("/")
    if not exists(gitea, f"/repos/{repo}"):
        gitea.request("POST", f"/orgs/{owner}/repos", json={"name": name, "default_branch": REPO_BRANCH, "private": False})
        print(f"created repo {repo}")
    if not exists(gitea, f"/repos/{repo}/branches/{REPO_BRANCH}"):
        push_demo_repo(gitea_url, repo)
        print(f"pushed demo_repo/ to {repo} {REPO_BRANCH} at {first_push_head(gitea, repo)[:7]}")


def first_push_head(gitea: GiteaClient, repo: str) -> str:
    # Gitea 1.24 panics (HTTP 500) on the branch API until it clears the repo's "empty" flag after the first push.
    for _ in range(FIRST_PUSH_ATTEMPTS - 1):
        try:
            return gitea.branch_head(repo, REPO_BRANCH)
        except GiteaError:
            time.sleep(FIRST_PUSH_POLL_S)
    return gitea.branch_head(repo, REPO_BRANCH)


def ensure_hire(gitea: GiteaClient, team_id: int, hire: str, password: str) -> None:
    if not exists(gitea, f"/users/{hire}"):
        gitea.request(
            "POST",
            "/admin/users",
            json={
                "username": hire,
                "email": HIRE_EMAIL.format(hire=hire),
                "password": password,
                "must_change_password": False,
                "send_notify": False,
            },
        )
        print(f"created hire {hire}")
    gitea.request("PUT", f"/teams/{team_id}/members/{hire}")
    title = WELCOME_ISSUE_TITLE.format(hire=hire)
    if gitea.find_issue(REPO, title) is None:
        issue = gitea.create_issue(REPO, title, welcome_body())
        gitea.request("PATCH", f"/repos/{REPO}/issues/{issue['number']}", json={"assignees": [hire]})
        print(f"opened #{issue['number']} {title!r} assigned to {hire}")


def reset(gitea: GiteaClient) -> None:
    for hire in HIRES:
        if exists(gitea, f"/users/{hire}"):
            gitea.request("DELETE", f"/admin/users/{hire}", params={"purge": "true"})
            print(f"deleted hire {hire}")
    if exists(gitea, f"/repos/{REPO}"):
        gitea.request("DELETE", f"/repos/{REPO}")
        print(f"deleted repo {REPO}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete hires and the repo before seeding")
    args = parser.parse_args()
    config = settings()
    gitea = GiteaClient(config)
    try:
        if args.reset:
            reset(gitea)
        ensure_org(gitea)
        team_id = ensure_team(gitea)
        ensure_repo(gitea, config.gitea_url, REPO)
        for hire in HIRES:
            ensure_hire(gitea, team_id, hire, config.gitea_hire_password)
    finally:
        gitea.close()
    print("seeded")


if __name__ == "__main__":
    main()
