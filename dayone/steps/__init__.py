"""The ordered onboarding checklist: every id in STEP_IDS, in order, from the terminal and browser modules."""

from dayone.contracts import STEP_IDS, Step
from dayone.steps.browser_steps import BROWSER_STEPS
from dayone.steps.terminal_steps import TERMINAL_STEPS

_ALL = {**TERMINAL_STEPS, **BROWSER_STEPS}
_missing = [step_id for step_id in STEP_IDS if step_id not in _ALL]
_extra = sorted(set(_ALL) - set(STEP_IDS))
if _missing or _extra:
    raise ImportError(f"step modules do not match STEP_IDS: missing {_missing}, unknown {_extra}")

STEPS: tuple[Step, ...] = tuple(_ALL[step_id] for step_id in STEP_IDS)
