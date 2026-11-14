"""Gitea API client (admin token). Implements `contracts.Gitea`; the seed reuses `request`."""

from __future__ import annotations

from typing import Any

import httpx

from dayone.config import GITEA_TOKEN_FILE, Settings
from dayone.contracts import REPO, WELCOME_CHECKLIST, WELCOME_ISSUE_TITLE

API_PREFIX = "/api/v1"
REQUEST_TIMEOUT_S = 15.0
PAGE_LIMIT = 50
HTTP_NOT_FOUND = 404


class GiteaError(RuntimeError):
    """A Gitea API call failed. Carries the method, path, status and response body."""

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"Gitea {method} {path} -> HTTP {status}: {body[:500]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class GiteaNotFound(GiteaError):
    pass


class GiteaTokenMissing(RuntimeError):
    pass


class GiteaUnreachable(RuntimeError):
    pass


def welcome_body() -> str:
    """The seeded Welcome issue body: every WELCOME_CHECKLIST item, in order, unticked."""
    items = "\n".join(f"- [ ] {item}" for item in WELCOME_CHECKLIST.values())
    return f"Welcome to the engineering team! Work through this list on your first day.\n\n{items}\n"


def read_admin_token() -> str:
    if not GITEA_TOKEN_FILE.exists():
        raise GiteaTokenMissing(f"{GITEA_TOKEN_FILE} not found; run scripts/up.sh")
    return GITEA_TOKEN_FILE.read_text().strip()


class GiteaClient:
    def __init__(self, settings: Settings) -> None:
        self._http = httpx.Client(
            base_url=settings.gitea_url.rstrip("/") + API_PREFIX,
            headers={"Authorization": f"token {read_admin_token()}"},
            timeout=REQUEST_TIMEOUT_S,
        )

    def close(self) -> None:
        self._http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """JSON body of a 2xx response (None when empty). Raises GiteaNotFound / GiteaError otherwise."""
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as error:
            raise GiteaUnreachable(f"Gitea {method} {path}: {error}") from error
        if response.status_code == HTTP_NOT_FOUND:
            raise GiteaNotFound(method, path, response.status_code, response.text)
        if response.is_error:
            raise GiteaError(method, path, response.status_code, response.text)
        return response.json() if response.content else None

    def user(self, username: str) -> dict[str, Any]:
        return self.request("GET", f"/users/{username}")

    def user_keys(self, username: str) -> list[dict[str, Any]]:
        # Each key carries Gitea's own `fingerprint` ("SHA256:..."), the same form `ssh-keygen -lf` prints.
        return self.request("GET", f"/users/{username}/keys", params={"limit": PAGE_LIMIT})

    def branch_head(self, repo: str, branch: str) -> str:
        return self.request("GET", f"/repos/{repo}/branches/{branch}")["commit"]["id"]

    def find_issue(self, repo: str, title: str) -> dict[str, Any] | None:
        # No `q=` filter: Gitea's issue indexer is asynchronous and misses issues created a moment ago.
        page = 1
        while issues := self.request(
            "GET", f"/repos/{repo}/issues", params={"state": "all", "type": "issues", "limit": PAGE_LIMIT, "page": page}
        ):
            for issue in issues:
                if issue["title"] == title:
                    return issue
            page += 1
        return None

    def welcome_issue(self, username: str) -> dict[str, Any]:
        title = WELCOME_ISSUE_TITLE.format(hire=username)
        issue = self.find_issue(REPO, title)
        if issue is None:
            raise GiteaNotFound("GET", f"/repos/{REPO}/issues", HTTP_NOT_FOUND, f"no issue titled {title!r}")
        return issue

    def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})

    def reset_hire(self, username: str) -> None:
        """Back to the seeded state for one hire: no SSH keys, Welcome issue open with every item unticked."""
        for key in self.user_keys(username):
            self.request("DELETE", f"/admin/users/{username}/keys/{key['id']}")
        issue = self.welcome_issue(username)
        self.request("PATCH", f"/repos/{REPO}/issues/{issue['number']}", json={"body": welcome_body(), "state": "open"})
