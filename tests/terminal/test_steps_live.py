"""Every terminal step on a fresh container, against the real seeded Gitea.

Each snag step must fail its check after the naive run() with recognisable error text, then pass once the
fix memory would learn is applied by hand. Error texts are written to tests/terminal/errors/ for inspection.
"""

from pathlib import Path

import pytest

from dayone.contracts import REPO_DIR, STEP_IDS, CheckResult, Machine
from dayone.steps.terminal_steps import TERMINAL_STEPS
from dayone.terminal.ssh import ssh_auth_ok
from dayone.terminal.fingerprint import fingerprint

DEV_HIRE = "grace"

ERRORS_DIR = Path(__file__).parent / "errors"
FIX_TIMEOUT_S = 900.0
DEV_KEY_TITLE = "dayone-dev-terminal-tests"  # dev only: stands in for the browser's b_add_ssh_key
UV = "/root/.local/bin/uv"

# What each snag must say, and the fix memory would learn, per machine. None: the naive run already passes.
SNAGS: dict[Machine, dict[str, tuple[str, str] | None]] = {
    Machine.UBUNTU: {
        "t_runtime": (
            "requires-python = '>=3.12'",
            f"curl -LsSf https://astral.sh/uv/install.sh | sh && rm -rf .venv && {UV} venv --seed --python 3.12 .venv",
        ),
        "t_system_package": ("pg_config executable not found", "apt-get install -y libpq-dev gcc python3-dev"),
    },
    Machine.DEBIAN: {
        "t_runtime": (
            "requires-python = '>=3.12'",
            f"curl -LsSf https://astral.sh/uv/install.sh | sh && rm -rf .venv && {UV} venv --seed --python 3.12 .venv",
        ),
        "t_system_package": ("pg_config executable not found", "apt-get install -y libpq-dev gcc python3-dev"),
    },
    Machine.ALPINE: {
        "t_runtime": None,
        "t_system_package": ("pg_config executable not found", "apk add postgresql-dev gcc musl-dev python3-dev"),
    },
}
COMMON_SNAGS = {
    "t_settings": ("settings.toml not found", "cp settings.example.toml settings.toml"),
    "t_init_db": ("no such table: users", ".venv/bin/python -m acme_api.db init"),
}


def _show(machine: Machine, step_id: str, label: str, result: CheckResult) -> None:
    print(f"\n[{machine}] {step_id} {label}: ok={result.ok} evidence={result.evidence!r}")
    if result.error_text:
        print(f"--- error_text (fingerprint {fingerprint(result.error_text)}) ---\n{result.error_text}\n---")


def _add_dev_key(gitea, public_key: str, fingerprint_: str) -> None:
    if any(key["fingerprint"] == fingerprint_ for key in gitea.user_keys(DEV_HIRE)):
        return
    for key in gitea.user_keys(DEV_HIRE):
        if key["title"] == DEV_KEY_TITLE:
            gitea.request("DELETE", f"/admin/users/{DEV_HIRE}/keys/{key['id']}")
    gitea.request("POST", f"/admin/users/{DEV_HIRE}/keys", json={"title": DEV_KEY_TITLE, "key": public_key})


# Reconciling: the test check re-runs the suite, so it legitimately passes before run() once setup is done.
PASSES_BEFORE_RUN = {"t_run_tests"}


@pytest.mark.parametrize("machine", [Machine.UBUNTU, Machine.ALPINE, Machine.DEBIAN])
def test_fresh_machine_reaches_passing_tests(machine: Machine, gitea, make_executor, make_context) -> None:
    executor = make_executor(machine)
    executor.remove_container()
    executor.ensure_container()
    snags = SNAGS[machine] | COMMON_SNAGS
    ERRORS_DIR.mkdir(exist_ok=True)
    for step_id in (step_id for step_id in STEP_IDS if step_id.startswith("t_")):
        step = TERMINAL_STEPS[step_id]
        ctx = make_context(executor, gitea, step_id)

        before = step.check(ctx)
        _show(machine, step_id, "check before run", before)
        if step_id not in PASSES_BEFORE_RUN:
            assert not before.ok, f"{step_id} passed before anything ran: the check proves nothing"

        run = step.run(ctx)
        assert run.ok, f"{step_id} naive run failed:\n{run.output[-3000:]}"
        after = step.check(ctx)
        _show(machine, step_id, "check after naive run", after)

        snag = snags.get(step_id)
        if snag is None:
            assert after.ok, f"{step_id} should pass after its naive run:\n{after.error_text}"
        else:
            expected, fix = snag
            assert not after.ok, f"{step_id} snag did not surface on {machine}"
            assert expected in after.error_text, f"{step_id} error text lacks {expected!r}"
            (ERRORS_DIR / f"{machine}_{step_id}.txt").write_text(after.error_text + "\n")
            fixed = executor.exec(fix, FIX_TIMEOUT_S, REPO_DIR)
            assert fixed.ok, f"manual fix for {step_id} failed:\n{fixed.output[-3000:]}"
            after = step.check(ctx)
            _show(machine, step_id, "check after manual fix", after)
            assert after.ok, f"{step_id} still failing after fix:\n{after.error_text}"

        if step_id == "t_ssh_keygen":
            denied = ssh_auth_ok(ctx)
            _show(machine, step_id, "ssh -T before key is registered", denied)
            _add_dev_key(gitea, after.detail["public_key"], after.detail["fingerprint"])
            authed = ssh_auth_ok(ctx)
            _show(machine, step_id, "ssh -T after key is registered", authed)
            assert authed.ok and not denied.ok

    assert TERMINAL_STEPS["t_run_tests"].check(make_context(executor, gitea, "t_run_tests")).evidence.endswith(
        "passed, exit 0"
    )
    # Re-running clone on an existing, current checkout fast-forwards instead of failing on the directory.
    clone_ctx = make_context(executor, gitea, "t_clone")
    assert TERMINAL_STEPS["t_clone"].run(clone_ctx).ok
    assert TERMINAL_STEPS["t_clone"].check(clone_ctx).ok
    # Key generation is idempotent: a second run keeps the registered key.
    key_ctx = make_context(executor, gitea, "t_ssh_keygen")
    first = TERMINAL_STEPS["t_ssh_keygen"].check(key_ctx).detail["fingerprint"]
    assert TERMINAL_STEPS["t_ssh_keygen"].run(key_ctx).ok
    assert TERMINAL_STEPS["t_ssh_keygen"].check(key_ctx).detail["fingerprint"] == first
