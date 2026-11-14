"""The headed Chromium the agent drives, with locator memory and the intern overlay."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from dayone.browser.locators import ISSUES_OPEN_WELCOME, SEED_LOCATORS, SEED_SOURCE, checklist_goal
from dayone.config import ROOT, STATE_DIR, Settings
from dayone.contracts import LLM, REPO, WELCOME_ISSUE_TITLE, EventBus, EventType, Hire, LocatorHint, Memory

BBox = tuple[float, float, float, float]

OVERLAY_SCRIPT = ROOT / "intern_overlay.js"
PROFILE_DIR = STATE_DIR / "browser"  # one persistent profile per hire, so the sign-in survives kill -9
VIEWPORT = {"width": 1440, "height": 900}
SLOW_MO_MS = 120
OVERLAY_HOLD_MS = 700  # how long the audience sees a ring before the click lands
MOVED_HOLD_MS = 1200
LOCATOR_TIMEOUT_MS = 2000
REASONING_ATTEMPTS = 3
REASONING_MAX_TOKENS = 200
SNAPSHOT_MAX_CHARS = 15000
IS_CHECKABLE_JS = """e => (e instanceof HTMLInputElement && ['checkbox', 'radio'].includes(e.type))
    || ['checkbox', 'radio'].includes(e.getAttribute('role'))"""
ISSUE_SAVE_SUFFIX = "/content"  # POST /{repo}/issues/{n}/content, sent by Gitea's task-list JS

REASONING_SYSTEM = (
    "You locate one element on a web page for a browser automation agent. "
    "You get the goal and the page's ARIA snapshot. Reply with exactly one Playwright selector on one line "
    "and nothing else: prefer role selectors such as role=button[name=\"Add Key\"] or "
    "role=textbox[name=\"Key Name\"]; CSS selectors also work. It must match exactly one visible element."
)


class Action(StrEnum):
    CLICK = "click"
    FILL = "fill"
    CHECK = "check"


class LocatorNotFound(RuntimeError):
    """Neither memory nor reasoning produced a locator that resolves to exactly one visible element."""


class ChecklistSaveFailed(RuntimeError):
    """Gitea answered the checkbox's save request with an error."""


class PlaywrightSession:
    def __init__(self, hire: Hire, settings: Settings, events: EventBus, memory: Memory, llm: LLM | None = None):
        self.hire = hire
        self.settings = settings
        self.events = events
        self.memory = memory
        self._llm = llm
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # --- lifecycle ------------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR / self.hire.name),
                headless=self.settings.browser_headless,
                slow_mo=SLOW_MO_MS,
                viewport=VIEWPORT,
                bypass_csp=True,
            )
            self._context.add_init_script(path=str(OVERLAY_SCRIPT))
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            from dayone.llm.anthropic import AnthropicLLM  # lazy: only hires whose locators fail need the LLM

            self._llm = AnthropicLLM(self.settings)
        return self._llm

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = self._playwright = self._page = None

    # --- navigation and read-back ---------------------------------------------

    def goto(self, path: str) -> None:
        self.page.goto(self.settings.gitea_url.rstrip("/") + path)

    def page_summary(self) -> str:
        path = self.page.url.removeprefix(self.settings.gitea_url.rstrip("/"))
        return f"{self.page.title()} ({path})"

    # --- acting through locator memory ------------------------------------------

    def act(self, goal: str, action: Action, step_id: str, value: str = "") -> Locator:
        """Act on `goal` from memory, then the hand-found seed (green); every hint that does not resolve to one
        element fit for `action` shows "This moved." (red); with none left, reason over the page (amber)."""
        summary = self.page_summary()
        for hint in self._hints(goal, summary):
            target = self._resolve(hint.locator, action)
            if target is None:
                self._mark_moved(hint)
                self._emit(step_id, goal, "moved", hint.locator, source_hire=hint.source_hire)
                continue
            bbox = self._mark(target, "memory", hint.source_hire)
            self._emit(step_id, goal, "memory", hint.locator, source_hire=hint.source_hire)
            self._perform(target, action, value)
            if hint.source_hire == SEED_SOURCE:
                self._save(goal, hint.locator, bbox, summary)
            return target
        locator, target = self._reason(goal, action, step_id)
        bbox = self._mark(target, "reasoning", "new locator")
        self._emit(step_id, goal, "reasoning", locator)
        self._perform(target, action, value)
        self._save(goal, locator, bbox, summary)
        return target

    def _hints(self, goal: str, summary: str) -> list[LocatorHint]:
        remembered = self.memory.find_locator(goal, summary)
        # A semantic hit for another goal on the same page (the username field for "remember me") is a
        # different element, never this one: only a locator saved for this exact goal is trusted.
        hints = [remembered] if remembered is not None and remembered.goal == goal else []
        seed = SEED_LOCATORS.get(goal)
        if seed is not None and all(hint.locator != seed for hint in hints):
            hints.append(LocatorHint(goal, seed, None, summary, SEED_SOURCE, None))
        return hints

    def _resolve(self, locator: str, action: Action) -> Locator | None:
        """The locator's element, if there is exactly one, it is visible, and `action` can be done to it."""
        try:
            target = self.page.locator(locator)
            target.first.wait_for(state="visible", timeout=LOCATOR_TIMEOUT_MS)
            if target.count() != 1:
                return None
            if action is Action.FILL:
                fits = target.is_editable()
            elif action is Action.CHECK:
                fits = target.evaluate(IS_CHECKABLE_JS)
            else:
                fits = target.is_enabled()
        except PlaywrightError:  # bad selector syntax, nothing visible in time, or it vanished meanwhile
            return None
        return target if fits else None

    def _reason(self, goal: str, action: Action, step_id: str) -> tuple[str, Locator]:
        self.events.emit(EventType.REASONING_STARTED, step_id=step_id, goal=goal)
        snapshot = self.page.locator("body").aria_snapshot()[:SNAPSHOT_MAX_CHARS]
        rejected: list[str] = []
        for _ in range(REASONING_ATTEMPTS):
            feedback = (
                f"\nThese answers did not match exactly one visible element you can {action}: {rejected}. Try another."
                if rejected
                else ""
            )
            prompt = (
                f"Goal: {goal} (to {action} it)\nPage: {self.page_summary()}\n\nARIA snapshot:\n{snapshot}\n{feedback}"
            )
            locator = _selector_line(self.llm.complete(REASONING_SYSTEM, prompt, REASONING_MAX_TOKENS))
            target = self._resolve(locator, action)
            if target is not None:
                return locator, target
            rejected.append(locator)
        raise LocatorNotFound(f"no locator for {goal!r} on {self.page_summary()}; rejected {rejected}")

    def _save(self, goal: str, locator: str, bbox: BBox | None, summary: str) -> None:
        hint = LocatorHint(goal, locator, bbox, summary, self.hire.name, None)
        self.memory.save_locator(hint, self.hire.name)

    @staticmethod
    def _perform(target: Locator, action: Action, value: str) -> None:
        if action is Action.FILL:
            target.fill(value)
        elif action is Action.CHECK:
            target.check()
        else:
            target.click()

    # --- overlay ----------------------------------------------------------------

    def _page_bbox(self, target: Locator) -> BBox | None:
        box = target.bounding_box()
        if box is None:
            return None
        scroll = self.page.evaluate("[window.scrollX, window.scrollY]")
        return (box["x"] + scroll[0], box["y"] + scroll[1], box["width"], box["height"])

    def _mark(self, target: Locator, mode: str, label: str | None) -> BBox | None:
        """Ring the target and return its page bbox (taken now: a click may navigate it away)."""
        target.scroll_into_view_if_needed()
        bbox = self._page_bbox(target)
        self.page.evaluate("o => window.__dayone.mark(o)", {"mode": mode, "bbox": bbox, "label": label})
        self.page.wait_for_timeout(OVERLAY_HOLD_MS)
        return bbox

    def _mark_moved(self, hint: LocatorHint) -> None:
        self.page.evaluate("o => window.__dayone.mark(o)", {"mode": "moved", "bbox": hint.bbox})
        self.page.wait_for_timeout(MOVED_HOLD_MS)

    def _emit(self, step_id: str, goal: str, mode: str, locator: str, **extra: object) -> None:
        self.events.emit(EventType.LOCATOR, step_id=step_id, goal=goal, mode=mode, locator=locator, **extra)

    # --- Welcome issue ----------------------------------------------------------

    def open_welcome(self, step_id: str) -> None:
        """Navigate like a person: search the repo's issues for the hire's Welcome issue and click it."""
        title = WELCOME_ISSUE_TITLE.format(hire=self.hire.name)
        # Gitea titles the page "#4 - Welcome to Acme, ada! - acme-api - ...", so the title only contains it.
        if self._page is not None and title in self.page.title():
            return
        self.goto(f"/{REPO}/issues?q={quote(title)}")
        self.act(ISSUES_OPEN_WELCOME, Action.CLICK, step_id=step_id)
        self.page.wait_for_load_state()

    def tick_item(self, item: str, step_id: str) -> None:
        self.open_welcome(step_id)
        # The issue's raw markdown is in the page; an already-ticked item fires no save to wait for.
        if is_ticked(self.page.locator(".raw-content").first.text_content() or "", item):
            return
        # Gitea blocks clicks on every box until the save returns, so each tick waits for its own save.
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(ISSUE_SAVE_SUFFIX)) as saved:
            self.act(checklist_goal(item), Action.CHECK, step_id=step_id)
        if not saved.value.ok:
            raise ChecklistSaveFailed(f"Gitea refused the tick of {item!r}: HTTP {saved.value.status}")


def is_ticked(markdown: str, item: str) -> bool:
    return f"- [x] {item}" in markdown


def _selector_line(reply: str) -> str:
    """The model sometimes explains first; the selector is its last line."""
    lines = [line.strip().strip("`") for line in reply.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""
