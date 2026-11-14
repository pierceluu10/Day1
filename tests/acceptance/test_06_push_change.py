"""Acceptance 6: `scripts/push_change.sh` lands mid-run; the agent notices, re-verifies, adapts and finishes.

Mutates Gitea: push_change.sh commits to acme/acme-api (scripts/reset.sh restores the repo).
"""

import subprocess

import pytest

from atlas import latest_run_id, ledger, of_type, run_events
from dayone.config import ROOT
from dayone.contracts import DONE_STATUSES, REPO, REPO_BRANCH, REPO_DIR, STEP_IDS, EventType, Machine, StepStatus
from harness import CLI_TIMEOUT_S, Agent, EventWatcher, container_exec, requires_system

PUSH_CHANGE = ROOT / "scripts" / "push_change.sh"

pytestmark = [
    pytest.mark.system,
    requires_system,
    pytest.mark.skipif(not PUSH_CHANGE.exists(), reason=f"needs {PUSH_CHANGE.relative_to(ROOT)} (not written yet)"),
]

CHANGE_AFTER = "t_clone"
RE_VERIFIED = ("t_clone", "t_install_deps", "t_run_tests")


def test_mid_run_repository_change_is_detected_and_handled(onboard, db, gitea, log_dir, fresh_hire, hires):
    hire, donor_name = hires["drift"], hires["first"]
    donor = onboard(donor_name, Machine.UBUNTU)
    assert donor.exit_code == 0, donor.output_tail

    fresh_hire(hire)
    watcher = EventWatcher(db, hire)
    agent = Agent(log_dir, "start", "--hire", hire, "--machine", Machine.UBUNTU.value)
    watcher.wait_for(
        agent,
        f"{CHANGE_AFTER} done",
        lambda event, current: event["type"] == EventType.STEP_STATUS
        and event.get("step_id") == CHANGE_AFTER
        and event["data"].get("status") in DONE_STATUSES,
    )
    head_before = gitea.branch_head(REPO, REPO_BRANCH)
    pushed = subprocess.run([str(PUSH_CHANGE)], cwd=ROOT, capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
    print(f"push_change.sh exit {pushed.returncode}:\n{pushed.stdout}{pushed.stderr}")
    assert pushed.returncode == 0
    head_after = gitea.branch_head(REPO, REPO_BRANCH)
    assert head_after != head_before, "push_change.sh did not move the branch HEAD"
    assert agent.wait() == 0, agent.output_tail()

    run_id = latest_run_id(db, hire)
    events = run_events(db, hire, run_id)
    types = [event["type"] for event in events]
    pushed_at = next(
        i for i, event in enumerate(events)
        if event["type"] == EventType.STEP_STATUS and event.get("step_id") == CHANGE_AFTER
    )
    drift = [(i, event) for i, event in enumerate(events) if event["type"] == EventType.DRIFT_DETECTED and i > pushed_at]
    print(f"run {run_id}: drift events {[event['data'] for _, event in drift]}")
    assert drift, "the agent never reported drift after the change landed"
    drift_at = drift[0][0]
    assert any((to := event["data"].get("to")) and head_after.startswith(to) for _, event in drift), f"no drift event names the new HEAD {head_after}"

    for step_id in RE_VERIFIED:
        assert any(
            event["type"] == EventType.STEP_STATUS and event.get("step_id") == step_id and event["data"].get("status") in DONE_STATUSES
            for event in events[drift_at:]
        ), f"{step_id} was not re-verified after drift"

    errors_after = [event for event in events[drift_at:] if event["type"] == EventType.ERROR_CAPTURED]
    print(f"errors after drift: {[(event['step_id'], event['data'].get('fingerprint')) for event in errors_after]}")
    assert errors_after, "the change introduced no new error: push_change.sh did not exercise adaptation"
    assert EventType.EXPERIENCE_SAVED in types[drift_at:], "the fix for the new error was not saved"

    steps = ledger(db, hire)
    for step_id in STEP_IDS:
        assert steps[step_id]["status"] in DONE_STATUSES, (step_id, steps[step_id]["status"])
        assert steps[step_id]["status"] != StepStatus.STALE
    local = container_exec(hire, f"git -C {REPO_DIR} rev-parse HEAD")
    assert local.stdout.strip() == head_after, f"container HEAD {local.stdout.strip()} != API HEAD {head_after}"
    assert of_type(events, EventType.RUN_FINISHED)
