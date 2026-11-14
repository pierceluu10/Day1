"""Acceptance 2: `kill -9` at five points chosen from live events, `dayone resume` after each.

Asserts: no step's run() starts again once the step is done (unless drift invalidated it first);
a step left `pending` by a kill gets a check result before any re-run; the ledger's run_count agrees
with the event log; the final state matches a clean run of the same hire (statuses may differ only
by a step being `skipped`; evidence equal once key fingerprints and durations are masked).

Mutates Gitea: commits one file to acme/acme-api to cause drift (scripts/reset.sh restores the repo).
"""

from __future__ import annotations

import base64
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from atlas import ledger, of_type, run_events
from dayone.contracts import DONE_STATUSES, REPO, REPO_BRANCH, STEP_IDS, EventType, Machine, StepStatus
from harness import Agent, EventWatcher, requires_system

pytestmark = [pytest.mark.system, requires_system]

DRIFT_PATH = "qa/drift-{ns}.md"
DOWNSTREAM_OF_HEAD = STEP_IDS[STEP_IDS.index("t_clone") :]
TERMINAL_HEAVY_STEPS = frozenset({"t_install_deps", "t_run_tests"})
VOLATILE = (
    (re.compile(r"SHA256:[A-Za-z0-9+/=]+"), "SHA256:<key>"),
    (re.compile(r"\d{4}-\d\d-\d\d[T ][\d:.]+Z?"), "<time>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?m?s\b"), "<duration>"),
)

Predicate = Callable[[dict[str, Any], str | None], bool]


@dataclass(frozen=True)
class KillPoint:
    name: str
    predicate: Predicate
    drift_before_resume: bool = False  # the drift commit lands while the agent is dead


def is_event(type: EventType, step_id: str | None = None) -> Predicate:
    return lambda event, current: event["type"] == type.value and (step_id is None or event.get("step_id") == step_id)


KILL_POINTS = (
    KillPoint("right after step_pending for t_base_tools", is_event(EventType.STEP_PENDING, "t_base_tools")),
    KillPoint("during browser step b_add_ssh_key", is_event(EventType.STEP_PENDING, "b_add_ssh_key")),
    KillPoint("during memory resolution", is_event(EventType.MEMORY_HIT)),
    KillPoint(
        "mid terminal output",
        lambda event, current: event["type"] == EventType.TERMINAL_OUTPUT and current in TERMINAL_HEAVY_STEPS,
        drift_before_resume=True,
    ),
    KillPoint("between drift and re-verify", is_event(EventType.DRIFT_DETECTED)),
)


def mask(evidence: str) -> str:
    for pattern, replacement in VOLATILE:
        evidence = pattern.sub(replacement, evidence)
    return evidence


def push_drift_commit(gitea) -> str:
    path = DRIFT_PATH.format(ns=time.time_ns())
    gitea.request(
        "POST",
        f"/repos/{REPO}/contents/{path}",
        json={
            "content": base64.b64encode(b"QA drift marker\n").decode(),
            "message": f"qa: drift marker {path}",
            "branch": REPO_BRANCH,
        },
    )
    head = gitea.branch_head(REPO, REPO_BRANCH)
    print(f"pushed {path}; {REPO} {REPO_BRANCH} HEAD is now {head}")
    return head


def invalidated_by(event: dict[str, Any]) -> list[str]:
    """Steps an event says are no longer done (so a re-run of them is legitimate)."""
    if event["type"] == EventType.STEP_STATUS and event["data"].get("status") == StepStatus.STALE:
        return [event["step_id"]]
    if event["type"] == EventType.DRIFT_DETECTED:
        return [event["step_id"]] if event.get("step_id") else list(DOWNSTREAM_OF_HEAD)
    return []


def reruns_after_done(events: list[dict[str, Any]]) -> list[str]:
    done: set[str] = set()
    violations = []
    for index, event in enumerate(events):
        step_id = event.get("step_id")
        if event["type"] == EventType.STEP_STATUS and event["data"].get("status") in DONE_STATUSES:
            done.add(step_id)
        done -= set(invalidated_by(event))
        if event["type"] == EventType.STEP_PENDING and step_id in done:
            violations.append(f"event {index}: {step_id} went pending again after it was done, with no drift between")
    return violations


def check_before_rerun(segment: list[dict[str, Any]], step_id: str) -> str | None:
    """None when the pending step's check result comes before any new intent for it in this resume."""
    types = [event["type"] for event in segment if event.get("step_id") == step_id]
    if EventType.STEP_CHECK_RESULT not in types:
        return f"{step_id} was pending at the kill but the resume never checked it"
    if EventType.STEP_PENDING in types and types.index(EventType.STEP_PENDING) < types.index(EventType.STEP_CHECK_RESULT):
        return f"{step_id} was re-run before it was checked"
    return None


def print_ledger(title: str, steps: dict[str, dict[str, Any]]) -> None:
    print(title)
    for step_id in STEP_IDS:
        doc = steps.get(step_id, {})
        print(f"  {step_id:18} {doc.get('status')!s:20} runs={doc.get('run_count')} | {doc.get('evidence')}")


def test_kill_nine_at_five_points_then_resume(onboard, db, gitea, log_dir, fresh_hire, hires):
    hire, donor_name = hires["killed"], hires["first"]
    donor = onboard(donor_name, Machine.UBUNTU)
    assert donor.exit_code == 0, f"{donor_name}'s run must succeed first so {hire} resolves from memory"

    fresh_hire(hire)
    watcher = EventWatcher(db, hire)
    agent = Agent(log_dir, "start", "--hire", hire, "--machine", Machine.UBUNTU.value)
    pending_at_kill: list[set[str]] = []
    for point in KILL_POINTS:
        watcher.wait_for(agent, point.name, point.predicate)
        agent.kill()
        steps = ledger(db, hire)
        pending = {step_id for step_id, doc in steps.items() if doc["status"] == StepStatus.PENDING}
        pending_at_kill.append(pending)
        print_ledger(f"after kill -9 {point.name}: pending {sorted(pending)}", steps)
        if point.drift_before_resume:
            push_drift_commit(gitea)
        agent = Agent(log_dir, "resume", "--hire", hire)
    assert agent.wait() == 0, agent.output_tail()

    killed = ledger(db, hire)
    print_ledger("final ledger after five kills", killed)
    run_ids = {event["run_id"] for event in db.events.find({"hire": hire}, {"run_id": 1})}
    assert len(run_ids) == 1, f"resume must keep the run_id, saw {run_ids}"
    events = run_events(db, hire, run_ids.pop())

    assert reruns_after_done(events) == []

    pending_counts = Counter(event["step_id"] for event in of_type(events, EventType.STEP_PENDING))
    for step_id in STEP_IDS:
        assert killed[step_id]["status"] in DONE_STATUSES, f"{step_id}: {killed[step_id]['status']}"
        assert killed[step_id]["run_count"] == pending_counts[step_id], (
            f"{step_id}: ledger run_count {killed[step_id]['run_count']} but {pending_counts[step_id]} step_pending events"
        )

    resumed_at = [i for i, event in enumerate(events) if event["type"] == EventType.RUN_RESUMED]
    assert len(resumed_at) == len(KILL_POINTS), f"expected {len(KILL_POINTS)} run_resumed events, saw {len(resumed_at)}"
    bounds = [*resumed_at, len(events)]
    problems = [
        problem
        for k, pending in enumerate(pending_at_kill)
        for step_id in sorted(pending)
        if (problem := check_before_rerun(events[bounds[k] : bounds[k + 1]], step_id))
    ]
    assert problems == []
    assert any(pending_at_kill), "no kill left a step pending: the kill points did not land mid-step"

    # Reference: the same hire, same repo HEAD, same memory, never killed.
    fresh_hire(hire)
    reference_agent = Agent(log_dir, "start", "--hire", hire, "--machine", Machine.UBUNTU.value)
    assert reference_agent.wait() == 0, reference_agent.output_tail()
    clean = ledger(db, hire)
    print_ledger("clean reference run", clean)
    differences = [
        f"{step_id}: killed {killed[step_id]['status']} vs clean {clean[step_id]['status']}"
        for step_id in STEP_IDS
        if killed[step_id]["status"] != clean[step_id]["status"] and StepStatus.SKIPPED not in (killed[step_id]["status"], clean[step_id]["status"])
    ] + [
        f"{step_id} evidence: killed {killed[step_id]['evidence']!r} vs clean {clean[step_id]['evidence']!r}"
        for step_id in STEP_IDS
        if mask(killed[step_id]["evidence"]) != mask(clean[step_id]["evidence"])
    ]
    assert differences == []
