"""Reasoning in a real container: the fix is only discoverable by reading a repo file and seeing its output."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from bson import ObjectId

from dayone.contracts import REPO_DIR, CheckResult, Failure, FixProposal, FixSource, MachineFacts, StepKind
from dayone.memory.experience import FIX_OUTPUT, MAX_REASONING_ATTEMPTS

IMAGE = "alpine:3.20"
COMMAND_TIMEOUT_S = 60
STEP = "t_init_db"
INIT_COMMAND = "./manage.sh provision-storage"
SCHEMA_ERROR = "sqlite3.OperationalError: no such table: users"

# Unknown subcommands exit 2 silently, so the init command can only be learnt by reading manage.sh.
MANAGE_SH = f"""#!/bin/sh
case "$1" in
  test)
    grep -q "schema=v3" var/acme.db 2>/dev/null || {{ echo "{SCHEMA_ERROR}"; exit 1; }}
    echo "14 passed";;
  provision-storage)
    mkdir -p var && echo "schema=v3 seeded=$(date +%s)" > var/acme.db && echo "storage provisioned";;
  *)
    exit 2;;
esac
"""
README = "# acme-api\\n\\nA demo service. Run the tests with `./manage.sh test`.\\n"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=check)


@pytest.fixture
def container(tmp_path: Path):
    name = f"dayone-memtest-{uuid.uuid4().hex[:8]}"
    (tmp_path / "manage.sh").write_text(MANAGE_SH)
    (tmp_path / "README.md").write_text(README)
    docker("run", "-d", "--name", name, IMAGE, "sleep", "600")
    try:
        docker("exec", name, "mkdir", "-p", REPO_DIR)
        docker("cp", f"{tmp_path}/.", f"{name}:{REPO_DIR}")
        docker("exec", name, "chmod", "+x", f"{REPO_DIR}/manage.sh")
        yield name
    finally:
        docker("rm", "-f", name, check=False)


def exec_in(container: str, command: str) -> dict:
    result = docker("exec", "-w", REPO_DIR, container, "sh", "-lc", command, check=False)
    return {"command": command, "exit_code": result.returncode, "output": result.stdout + result.stderr}


def test_reasoning_learns_the_init_command_from_inspected_output(harness, container):
    memory, llm, _, _, experiences = harness
    calls: list[FixProposal] = []

    def try_fix(proposal: FixProposal) -> CheckResult:
        """Same contract as the engine's _try_fix: stop at the first failing command, then run the step's check."""
        calls.append(proposal)
        results = []
        for command in proposal.commands:
            results.append(exec_in(container, command))
            if results[-1]["exit_code"] != 0:
                return CheckResult(
                    ok=False,
                    evidence=f"fix command failed: {command}",
                    detail={FIX_OUTPUT: results},
                    error_text=results[-1]["output"] or f"exit {results[-1]['exit_code']}",
                )
        check = exec_in(container, "./manage.sh test")
        return CheckResult(
            ok=check["exit_code"] == 0,
            evidence=check["output"].strip().splitlines()[-1] if check["output"].strip() else "no output",
            detail={FIX_OUTPUT: results},
            error_text="" if check["exit_code"] == 0 else check["output"],
        )

    failure = Failure(
        hire="ada",
        step_id=STEP,
        kind=StepKind.TERMINAL,
        error_text=SCHEMA_ERROR,
        fingerprint="init-db-schema",
        facts=MachineFacts("alpine", "3.20", "apk", {}),
    )
    resolution = memory.resolve(failure, try_fix)

    for number, call in enumerate(calls, 1):
        print(f"attempt {number}: {call.commands}")
    assert resolution.fixed, resolution
    assert resolution.proposal.source == FixSource.REASONING
    assert resolution.attempts <= MAX_REASONING_ATTEMPTS
    assert resolution.last_check.evidence == "14 passed"
    assert any(INIT_COMMAND in command for command in resolution.proposal.commands)
    first_use = next(n for n, call in enumerate(calls) if any(INIT_COMMAND in command for command in call.commands))
    if first_use:
        assert "provision-storage)" in llm.prompts[first_use], "manage.sh as inspected must reach the attempt using it"
    saved = experiences.find_one({"_id": ObjectId(resolution.proposal.experience_id)})
    print("saved", saved["fix_commands"])
    assert any(INIT_COMMAND in command for command in saved["fix_commands"])
    assert not any(command.endswith("|| true") for command in saved["fix_commands"]), "inspection is not the fix"
