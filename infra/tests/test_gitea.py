"""GiteaClient against the live, seeded Gitea (scripts/up.sh). Uses a throwaway user, never a hire."""

import subprocess

import pytest

from dayone.config import settings
from dayone.contracts import HIRES, REPO, REPO_BRANCH, WELCOME_ISSUE_TITLE
from dayone.gitea import GiteaClient, GiteaNotFound, welcome_body

PROBE = "dayone-probe"
TICKED = welcome_body().replace("- [ ]", "- [x]")


@pytest.fixture
def gitea():
    client = GiteaClient(settings())
    yield client
    client.close()


@pytest.fixture
def probe(gitea, tmp_path):
    """A user with one SSH key and a fully ticked, closed Welcome issue."""
    gitea.request(
        "POST",
        "/admin/users",
        json={"username": PROBE, "email": f"{PROBE}@acme.test", "password": settings().gitea_hire_password,
              "must_change_password": False, "send_notify": False},
    )
    key_path = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", PROBE, "-f", str(key_path)], check=True)
    gitea.request("POST", f"/admin/users/{PROBE}/keys", json={"title": PROBE, "key": key_path.with_suffix(".pub").read_text()})
    fingerprint = subprocess.run(["ssh-keygen", "-lf", str(key_path)], check=True, capture_output=True, text=True).stdout.split()[1]
    issue = gitea.create_issue(REPO, WELCOME_ISSUE_TITLE.format(hire=PROBE), TICKED)
    gitea.request("PATCH", f"/repos/{REPO}/issues/{issue['number']}", json={"state": "closed"})
    yield fingerprint
    gitea.request("DELETE", f"/repos/{REPO}/issues/{issue['number']}")
    gitea.request("DELETE", f"/admin/users/{PROBE}", params={"purge": "true"})


def test_user_keys_carry_the_ssh_keygen_fingerprint(gitea, probe):
    keys = gitea.user_keys(PROBE)
    print("keys:", [(key["title"], key["fingerprint"]) for key in keys])
    assert [key["fingerprint"] for key in keys] == [probe]


def test_reset_hire_removes_keys_and_unticks_the_welcome_issue_idempotently(gitea, probe):
    for _ in range(2):
        gitea.reset_hire(PROBE)
        issue = gitea.welcome_issue(PROBE)
        print("after reset:", issue["state"], gitea.user_keys(PROBE), repr(issue["body"]))
        assert gitea.user_keys(PROBE) == []
        assert issue["body"] == welcome_body() and issue["state"] == "open"


def test_seeded_hires_have_a_welcome_issue_assigned_to_them(gitea):
    for hire in HIRES:
        issue = gitea.welcome_issue(hire)
        assert [assignee["login"] for assignee in issue["assignees"]] == [hire]
        assert gitea.user(hire)["login"] == hire


def test_branch_head_is_a_full_sha(gitea):
    head = gitea.branch_head(REPO, REPO_BRANCH)
    print("head:", head)
    assert len(head) == 40 and int(head, 16) >= 0


def test_missing_things_raise_or_return_none(gitea):
    with pytest.raises(GiteaNotFound):
        gitea.user("no-such-user-dayone")
    with pytest.raises(GiteaNotFound):
        gitea.welcome_issue("no-such-user-dayone")
    assert gitea.find_issue(REPO, "no such issue title dayone") is None
