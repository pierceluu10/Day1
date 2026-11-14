"""Browser steps against the live Gitea as linus, headless. Each check must fail before its run and pass after."""

from __future__ import annotations

import pytest

from dayone.browser.locators import (
    KEYS_OPEN_FORM,
    LOGIN_PASSWORD,
    LOGIN_REMEMBER,
    LOGIN_SUBMIT,
    SEED_LOCATORS,
    SEED_SOURCE,
    checklist_goal,
)
from dayone.browser.session import Action
from dayone.contracts import GITEA_HOSTNAME, LocatorHint, REPO, REPO_DIR, WELCOME_CHECKLIST
from dayone.steps.browser_steps import BROWSER_STEPS

from conftest import HIRE, ClaudeLLM, DictMemory, RecordingEvents

CLONE = f"GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new' git clone git@{GITEA_HOSTNAME}:{REPO}.git {REPO_DIR}"


def reconcile(step_id, ctx):
    """What the engine does: check, run, check. Returns (check before, check after)."""
    step = BROWSER_STEPS[step_id]
    before = step.check(ctx)
    print(step_id, "before:", before)
    assert not before.ok, f"{step_id} check passed before run: it cannot tell done from not done"
    run = step.run(ctx)
    print(step_id, "run:", run)
    after = step.check(ctx)
    print(step_id, "after:", after)
    return before, after


def sign_in(make_context, memory, events):
    ctx = make_context("b_sign_in", memory, events)
    _, after = reconcile("b_sign_in", ctx)
    assert after.ok, after
    return ctx


def test_ssh_key_added_in_browser_lets_container_clone(clean_linus, make_context, gitea, screenshots):
    memory, events = DictMemory(), RecordingEvents()
    signed_in = sign_in(make_context, memory, events)
    ctx = make_context("b_add_ssh_key", memory, events)
    ctx.browser = signed_in.browser  # one browser per run
    shots = screenshots(ctx.browser)

    before, after = reconcile("b_add_ssh_key", ctx)
    assert "does not list key" in before.evidence
    assert after.ok, after
    assert "API lists key SHA256:" in after.evidence and "authenticated" in after.evidence

    keys = gitea.user_keys(HIRE)
    assert [key["title"] for key in keys] == [ctx.idempotency_key]
    clone = ctx.executor.exec(CLONE, 120)
    assert clone.ok, clone.output
    head = ctx.executor.exec("git rev-parse HEAD", 30, workdir=REPO_DIR).output.strip()
    assert head == gitea.branch_head(REPO, "main")

    rerun = BROWSER_STEPS["b_add_ssh_key"].run(ctx)
    assert "not adding twice" in rerun.output and len(gitea.user_keys(HIRE)) == 1

    # Hire 1 had nothing in memory: every element came from the hand-found seeds, and each was saved.
    assert set(events.modes()) == {"memory"}
    assert {hint.goal for hint, _ in memory.saved} >= {KEYS_OPEN_FORM, LOGIN_SUBMIT}
    print([(hint.goal, hint.bbox, hire) for hint, hire in memory.saved])
    assert all(hint.bbox and hire == HIRE for hint, hire in memory.saved)
    assert any("memory" in path for path in shots)


def test_sign_in_survives_browser_restart(clean_linus, make_context):
    memory, events = DictMemory(), RecordingEvents()
    first = sign_in(make_context, memory, events)
    first.browser.close()  # same as the process dying: only the on-disk profile remains
    resumed = make_context("b_sign_in", memory, events)
    check = BROWSER_STEPS["b_sign_in"].check(resumed)
    print(check)
    assert check.ok, "a resumed run would sign in again: the session did not persist"


def test_moved_element_is_recovered_by_reasoning_and_saved(clean_linus, make_context, screenshots):
    memory, events = DictMemory(), RecordingEvents()
    llm = ClaudeLLM()
    signed_in = sign_in(make_context, memory, events)
    browser = signed_in.browser
    browser._llm = llm
    shots = screenshots(browser)
    stale = memory.hints[LOGIN_SUBMIT]  # a real saved hint, reused for its shape
    memory.hints[KEYS_OPEN_FORM] = stale.__class__(KEYS_OPEN_FORM, "#add-ssh-button", (1180, 300, 90, 32),
                                                    "SSH / GPG Keys", "ada", 0.91)

    browser.goto("/user/settings/keys")
    # What scripts/move_ui.sh does to the page: the button the saved locator points at is gone.
    browser.page.evaluate("document.querySelector('#add-ssh-button').id = 'ssh-add-key-v2'")
    target = browser.act(KEYS_OPEN_FORM, Action.CLICK, step_id="b_add_ssh_key")

    assert events.modes(KEYS_OPEN_FORM) == ["moved", "reasoning"]
    assert len(llm.prompts) >= 1 and "Add Key" in llm.prompts[0]
    assert browser.page.locator("#ssh-key-title").is_visible(), "the reasoned locator did not open the form"
    new = memory.hints[KEYS_OPEN_FORM]
    assert new.locator != "#add-ssh-button" and new.source_hire == HIRE
    assert target.count() == 1
    assert any("moved" in path for path in shots) and any("reasoning" in path for path in shots)

    # The next hire finds the reasoned locator in memory: green, no LLM call.
    calls = len(llm.prompts)
    browser.goto("/user/settings/keys")
    browser.page.evaluate("document.querySelector('#add-ssh-button').id = 'ssh-add-key-v2'")
    browser.act(KEYS_OPEN_FORM, Action.CLICK, step_id="b_add_ssh_key")
    assert events.modes(KEYS_OPEN_FORM)[-1] == "memory" and len(llm.prompts) == calls


@pytest.mark.parametrize(
    ("goal", "action", "wrong"),
    [
        (LOGIN_REMEMBER, Action.CHECK, "#user_name"),  # what memory really returned in the first integrated run
        (LOGIN_PASSWORD, Action.FILL, 'role=button[name="Sign In"]'),
        (LOGIN_SUBMIT, Action.CLICK, "#no-such-button"),
    ],
)
def test_remembered_locator_unfit_for_action_is_treated_as_moved(clean_linus, make_context, goal, action, wrong):
    memory, events = DictMemory(), RecordingEvents()
    memory.hints[goal] = LocatorHint(goal, wrong, (507.0, 147.0, 426.0, 38.0), "Sign In", "ada", None)
    browser = make_context("b_sign_in", memory, events).browser
    browser.goto("/user/login")

    target = browser.act(goal, action, "secret" if action is Action.FILL else "", step_id="b_sign_in")

    modes = [(d["mode"], d["locator"]) for t, _, d in events.events if t == "locator"]
    print(modes)
    assert modes == [("moved", wrong), ("memory", SEED_LOCATORS[goal])]
    if action is Action.CHECK:
        assert target.is_checked()
    if action is Action.FILL:
        assert target.input_value() == "secret"
    assert memory.hints[goal].locator == SEED_LOCATORS[goal] and memory.hints[goal].source_hire == HIRE


def test_welcome_issue_and_checklist(clean_linus, make_context, gitea):
    memory, events = DictMemory(), RecordingEvents()
    signed_in = sign_in(make_context, memory, events)
    ctx = make_context("b_open_welcome", memory, events)
    ctx.browser = signed_in.browser
    first = WELCOME_CHECKLIST["b_add_ssh_key"]
    page_before = ctx.browser.page.url

    # The engine ticks as soon as b_add_ssh_key verifies, before b_open_welcome: that must not open the issue.
    ctx.browser.tick_checklist(first)
    assert ctx.browser.page.url == page_before, "tick_checklist opened the issue on the hire's behalf"
    assert "- [x]" not in gitea.welcome_issue(HIRE)["body"]

    _, opened = reconcile("b_open_welcome", ctx)
    assert opened.ok, opened
    assert f"#{gitea.welcome_issue(HIRE)['number']}" in opened.evidence
    body = gitea.welcome_issue(HIRE)["body"]
    print(body)
    assert f"- [x] {first}" in body and body.count("- [x]") == 1, "the deferred tick was not done on open"

    ctx.browser.tick_checklist(first)  # already ticked is fine
    assert gitea.welcome_issue(HIRE)["body"].count("- [x]") == 1

    ctx.step_id = "b_checklist"
    before, after = reconcile("b_checklist", ctx)
    assert before.detail["ticked"] == 1
    assert after.ok, after
    assert events.modes(checklist_goal(first))[0] == "memory"
    assert memory.hints[checklist_goal(first)].source_hire in {HIRE, SEED_SOURCE}

    # After a restart (blank tab) the open is still proven: the read state lives in Gitea, not the page.
    ctx.browser.close()
    resumed = make_context("b_open_welcome", memory, events)
    assert BROWSER_STEPS["b_open_welcome"].check(resumed).ok
