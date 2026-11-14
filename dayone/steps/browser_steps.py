"""Browser steps: each acts in the hire's Chromium and is checked through a different route."""

from __future__ import annotations

from dayone.browser.locators import (
    KEYS_CONTENT,
    KEYS_OPEN_FORM,
    KEYS_SUBMIT,
    KEYS_TITLE,
    LOGIN_PASSWORD,
    LOGIN_REMEMBER,
    LOGIN_SUBMIT,
    LOGIN_USERNAME,
)
from dayone.browser.session import Action, PlaywrightSession, is_ticked
from dayone.config import settings
from dayone.contracts import (
    REPO,
    WELCOME_CHECKLIST,
    CheckResult,
    RunResult,
    Step,
    StepContext,
    StepKind,
)
from dayone.gitea import GiteaClient
from dayone.terminal.ssh import ssh_auth_ok

PUBLIC_KEY_PATH = "~/.ssh/id_ed25519.pub"
SIGNED_IN_MARKER = "Signed in as <strong>{hire}</strong>"
HTTP_OK = 200
SIGNED_IN_ONLY_PATH = "/user/settings"
NOTIFICATION_LIMIT = 50


def _browser(ctx: StepContext) -> PlaywrightSession:
    return ctx.browser  # type: ignore[return-value]  # steps need the page, which only the concrete class has


# --- b_sign_in ------------------------------------------------------------------


def sign_in_run(ctx: StepContext) -> RunResult:
    browser = _browser(ctx)
    browser.goto("/user/login")
    browser.act(LOGIN_USERNAME, Action.FILL, ctx.step_id, ctx.hire.name)
    browser.act(LOGIN_PASSWORD, Action.FILL, ctx.step_id, settings().gitea_hire_password)
    browser.act(LOGIN_REMEMBER, Action.CHECK, ctx.step_id)  # long-lived cookie survives a restart
    browser.act(LOGIN_SUBMIT, Action.CLICK, ctx.step_id)
    browser.page.wait_for_load_state()
    return RunResult(ok=True, output=f"submitted sign-in form, now at {browser.page.url}")


def sign_in_check(ctx: StepContext) -> CheckResult:
    """The browser's own cookie jar, replayed outside the page, must reach a signed-in-only page as the hire.

    Redirects are followed: after a restart Gitea turns the remember-me cookie back into a session on the
    way through /user/login, and an unknown browser ends on the login form, which lacks the marker.
    """
    response = _browser(ctx).page.context.request.get(settings().gitea_url.rstrip("/") + SIGNED_IN_ONLY_PATH)
    final_path = response.url.removeprefix(settings().gitea_url.rstrip("/"))
    signed_in = response.status == HTTP_OK and SIGNED_IN_MARKER.format(hire=ctx.hire.name) in response.text()
    detail = {"http_status": response.status, "final_path": final_path}
    if signed_in:
        return CheckResult(True, f"browser cookies: GET {final_path} {response.status}, signed in as {ctx.hire.name}", detail)
    error = f"GET {SIGNED_IN_ONLY_PATH} ended at {final_path} (HTTP {response.status}): not signed in as {ctx.hire.name}"
    return CheckResult(False, error, detail, error_text=error)


# --- b_add_ssh_key --------------------------------------------------------------


def _public_key(ctx: StepContext) -> str:
    result = ctx.executor.exec(f"cat {PUBLIC_KEY_PATH}", settings().exec_timeout_s)
    if not result.ok:
        raise FileNotFoundError(f"no public key in the container: {result.output.strip()}")
    return result.output.strip()


def add_ssh_key_run(ctx: StepContext) -> RunResult:
    if any(key["title"] == ctx.idempotency_key for key in ctx.gitea.user_keys(ctx.hire.name)):
        return RunResult(ok=True, output=f"key titled {ctx.idempotency_key} already added; not adding twice")
    public_key = _public_key(ctx)
    browser = _browser(ctx)
    browser.goto("/user/settings/keys")
    browser.act(KEYS_OPEN_FORM, Action.CLICK, ctx.step_id)
    browser.act(KEYS_TITLE, Action.FILL, ctx.step_id, ctx.idempotency_key)
    browser.act(KEYS_CONTENT, Action.FILL, ctx.step_id, public_key)
    browser.act(KEYS_SUBMIT, Action.CLICK, ctx.step_id)
    browser.page.wait_for_load_state()
    return RunResult(ok=True, output=f"submitted key {ctx.idempotency_key}")


def add_ssh_key_check(ctx: StepContext) -> CheckResult:
    listing = ctx.executor.exec(f"ssh-keygen -lf {PUBLIC_KEY_PATH}", settings().exec_timeout_s)
    if not listing.ok:
        return CheckResult(False, "no SSH key in the container", {"output": listing.output}, error_text=listing.output)
    fingerprint = listing.output.split()[1]
    listed = [key["fingerprint"] for key in ctx.gitea.user_keys(ctx.hire.name)]
    detail = {"fingerprint": fingerprint, "api_fingerprints": listed}
    if fingerprint not in listed:
        error = f"API does not list key {fingerprint} for {ctx.hire.name} (lists {listed or 'none'})"
        return CheckResult(False, error, detail, error_text=error)
    ssh = ssh_auth_ok(ctx)
    detail["ssh"] = ssh.evidence
    if not ssh.ok:
        return CheckResult(False, f"API lists key {fingerprint}; {ssh.evidence}", detail, error_text=ssh.error_text)
    return CheckResult(True, f"API lists key {fingerprint}; {ssh.evidence}", detail)


# --- b_open_welcome -------------------------------------------------------------


def open_welcome_run(ctx: StepContext) -> RunResult:
    browser = _browser(ctx)
    browser.open_welcome(ctx.step_id)
    return RunResult(ok=True, output=f"opened {browser.page.url}")


def open_welcome_check(ctx: StepContext) -> CheckResult:
    """Gitea marks the hire's notification for an issue read when the hire views that issue: server-side
    proof the hire opened it, read through the API as the hire (admin sudo), not from the page."""
    issue = ctx.gitea.welcome_issue(ctx.hire.name)
    gitea: GiteaClient = ctx.gitea  # type: ignore[assignment]  # sudo reads need the concrete client
    threads = gitea.request(
        "GET",
        "/notifications",
        params={"all": "true", "subject-type": "issue", "limit": NOTIFICATION_LIMIT, "sudo": ctx.hire.name},
    )
    issue_path = f"/{REPO}/issues/{issue['number']}"
    thread = next((t for t in threads if t["subject"]["html_url"].endswith(issue_path)), None)
    detail = {"api_number": issue["number"], "thread_id": thread and thread["id"], "unread": thread and thread["unread"]}
    if thread is None:
        error = f"no notification for Welcome issue #{issue['number']} in {ctx.hire.name}'s inbox (is it assigned?)"
        return CheckResult(False, error, detail, error_text=error)
    if thread["unread"]:
        error = f"Welcome issue #{issue['number']} is still unread for {ctx.hire.name}"
        return CheckResult(False, error, detail, error_text=error)
    return CheckResult(True, f"API: {ctx.hire.name} has read Welcome issue #{issue['number']} {issue['title']!r}", detail)


# --- b_checklist ----------------------------------------------------------------


def checklist_run(ctx: StepContext) -> RunResult:
    browser = _browser(ctx)
    for item in WELCOME_CHECKLIST.values():
        browser.tick_item(item, ctx.step_id)
    return RunResult(ok=True, output=f"ticked {len(WELCOME_CHECKLIST)} items")


def checklist_check(ctx: StepContext) -> CheckResult:
    body = ctx.gitea.welcome_issue(ctx.hire.name)["body"]
    missing = [item for item in WELCOME_CHECKLIST.values() if not is_ticked(body, item)]
    done = len(WELCOME_CHECKLIST) - len(missing)
    detail = {"ticked": done, "total": len(WELCOME_CHECKLIST), "unticked": missing}
    if missing:
        error = f"API issue body has {done}/{len(WELCOME_CHECKLIST)} ticked; unticked: {missing}"
        return CheckResult(False, error, detail, error_text=error)
    return CheckResult(True, f"API issue body: {done}/{len(WELCOME_CHECKLIST)} items ticked", detail)


BROWSER_STEPS: dict[str, Step] = {
    "b_sign_in": Step("b_sign_in", StepKind.BROWSER, "Sign in to Gitea", sign_in_check, sign_in_run),
    "b_add_ssh_key": Step("b_add_ssh_key", StepKind.BROWSER, "Add the laptop's SSH key", add_ssh_key_check, add_ssh_key_run),
    "b_open_welcome": Step("b_open_welcome", StepKind.BROWSER, "Open the Welcome issue", open_welcome_check, open_welcome_run),
    "b_checklist": Step("b_checklist", StepKind.BROWSER, "Tick the Welcome checklist", checklist_check, checklist_run),
}
