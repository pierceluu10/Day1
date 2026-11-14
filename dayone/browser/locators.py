"""Browser goals and the hand-found locators for Gitea 1.24.7 that seed locator memory.

A seed is tried after memory and before reasoning; when it works it is saved to memory under the
hire who used it, so later hires act from real memory. When the UI moves, the seed fails like any
stale locator: red "This moved.", then amber reasoning.
"""

from __future__ import annotations

from dayone.contracts import WELCOME_CHECKLIST

SEED_SOURCE = "hand-found"

LOGIN_USERNAME = "sign-in page: username field"
LOGIN_PASSWORD = "sign-in page: password field"
LOGIN_REMEMBER = "sign-in page: remember this device checkbox"
LOGIN_SUBMIT = "sign-in page: sign in button"
KEYS_OPEN_FORM = "ssh keys settings page: button that opens the add-key form"
KEYS_TITLE = "ssh keys settings page: key name field"
KEYS_CONTENT = "ssh keys settings page: key content field"
KEYS_SUBMIT = "ssh keys settings page: submit button of the add-key form"
ISSUES_OPEN_WELCOME = "issue search results: title link of the hire's Welcome issue"


def checklist_goal(item: str) -> str:
    return f"welcome issue page: task-list checkbox for '{item}'"


SEED_LOCATORS: dict[str, str] = {
    LOGIN_USERNAME: "#user_name",
    LOGIN_PASSWORD: "#password",
    LOGIN_REMEMBER: 'role=checkbox[name="Remember This Device"]',
    LOGIN_SUBMIT: 'role=button[name="Sign In"]',
    KEYS_OPEN_FORM: "#add-ssh-button",
    KEYS_TITLE: "#ssh-key-title",
    KEYS_CONTENT: "#ssh-key-content",
    KEYS_SUBMIT: "#add-ssh-key-panel button.primary",
    ISSUES_OPEN_WELCOME: "role=link[name=/^Welcome to Acme, /]",
    **{
        checklist_goal(item): f'.render-content li.task-list-item:has-text("{item}") input[type=checkbox]'
        for item in WELCOME_CHECKLIST.values()
    },
}
