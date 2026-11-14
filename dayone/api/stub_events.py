"""Dev only: write the demo's run-of-show into a dev Atlas database so the page can be built before the engine.

    python -m dayone.api.stub_events recorded   # ada, grace, linus finished earlier today (instant)
    python -m dayone.api.stub_events barbara    # live: memory fixes, then stops as if `kill -9`'d mid-step
    python -m dayone.api.stub_events resume     # live: barbara resumes, remote HEAD moves, reasoning, tests pass
    python -m dayone.api.stub_events ken        # live: alpine, apt fix found semantically and adapted to apk

Writes `events`, `steps` and `hires` in the shapes of `dayone.contracts`, exactly as the engine's store would.
Refuses to touch the integration database.
"""

from __future__ import annotations

import argparse
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.database import Database

from dayone.api import processes, queries
from dayone.config import settings
from dayone.contracts import (
    DONE_STATUSES,
    EventType,
    FixSource,
    Machine,
    StepStatus,
    container_name,
    idempotency_key,
)

INTEGRATION_DB = "dayone"
HEAD_BEFORE = "3f2a1c9"
HEAD_AFTER = "8be04d1"
KEY_FINGERPRINT = "SHA256:Xk3vT9pL2qR8mN4wYc7bE1sA6dF0gH5jK"

EVIDENCE = {
    "t_base_tools": "git 2.34.1, OpenSSH_8.9p1, curl 7.81.0",
    "t_ssh_keygen": f"{KEY_FINGERPRINT[:18]}… ed25519",
    "b_sign_in": "API /user via session cookie: {hire}",
    "b_add_ssh_key": f"API lists key {KEY_FINGERPRINT[:18]}…; ssh -T: authenticated",
    "b_open_welcome": "page issue #{issue} = API 'Welcome to Acme, {hire}!'",
    "t_clone": f"local HEAD {HEAD_BEFORE} = API HEAD {HEAD_BEFORE}",
    "t_runtime": "venv Python 3.12.7 satisfies >=3.12",
    "t_system_package": "psycopg2 2.9.10 wheel built",
    "t_install_deps": "pip check: no broken requirements; import acme_api ok",
    "t_settings": "settings.toml loaded (env=dev)",
    "t_init_db": "schema: users, projects, memberships",
    "t_run_tests": "14 passed, exit 0",
    "b_checklist": "API issue #{issue}: 6 of 6 items ticked",
}

RUN_COMMANDS = {
    "t_base_tools": "apt-get update && apt-get install -y git openssh-client curl ca-certificates",
    "t_ssh_keygen": "ssh-keygen -t ed25519 -N '' -C {key} -f /root/.ssh/id_ed25519",
    "t_clone": "git clone ssh://git@gitea:22/acme/acme-api.git /root/acme-api",
    "t_runtime": "python3 -m venv .venv",
    "t_system_package": ".venv/bin/pip wheel --no-deps -w /tmp/wheels psycopg2",
    "t_install_deps": ".venv/bin/pip install -r requirements.txt",
    "t_settings": "test -f settings.toml",
    "t_init_db": ".venv/bin/python -c 'import acme_api.db'",
    "t_run_tests": ".venv/bin/pytest -q",
}

ERRORS = {
    "t_runtime": "ERROR: Package 'acme-api' requires a different Python: 3.10.12 not in '>=3.12'",
    "t_system_package": "Error: pg_config executable not found.\npg_config is required to build psycopg2 from source.",
    "t_settings": "SettingsError: settings.toml not found (copy settings.example.toml)",
    "t_init_db": "sqlite3.OperationalError: no such table: users",
    "t_drift": "lxml: Error: Please make sure the libxml2 and libxslt development packages are installed.",
}

FIXES = {
    "t_runtime": ("uv python install 3.12", "uv venv --python 3.12 .venv"),
    "t_system_package": ("apt-get install -y libpq-dev gcc python3-dev",),
    "t_settings": ("cp settings.example.toml settings.toml",),
    "t_init_db": (".venv/bin/python -m acme_api.db init",),
    "t_drift": ("apt-get install -y libxml2-dev libxslt1-dev",),
}

LOCATORS = {
    "b_sign_in": ("login form: username field", "get_by_label('Username or Email Address')"),
    "b_add_ssh_key": ("ssh key form: content field", "get_by_label('Content')"),
    "b_open_welcome": ("issue list: welcome issue", "get_by_role('link', name='Welcome to Acme')"),
    "b_checklist": ("issue body: task list checkbox", "locator('.task-list-item input')"),
}


@dataclass
class Ledger:
    """Emits events and keeps `steps` in step with them, live (real sleeps) or recorded (virtual clock)."""

    db: Database
    hire: str
    machine: Machine
    run_id: str
    live: bool
    speed: float
    clock: datetime = field(default_factory=lambda: datetime.now(UTC))

    def wait(self, seconds: float) -> None:
        if self.live:
            time.sleep(seconds / self.speed)
            self.clock = datetime.now(UTC)
        else:
            self.clock += timedelta(seconds=seconds)

    def emit(self, type: EventType, step_id: str | None = None, **data: Any) -> None:
        self.db[queries.EVENTS].insert_one(
            {"hire": self.hire, "run_id": self.run_id, "type": type.value, "ts": self.clock, "step_id": step_id, "data": data}
        )

    def set_step(self, step_id: str, **fields: Any) -> None:
        runs = fields.pop("runs", 0)
        defaults = {
            "status": StepStatus.NOT_STARTED.value, "check_ever_failed": False, "evidence": "", "fix_source": None,
            "experience_id": None, "llm_calls": 0,
        }
        self.db[queries.STEPS].update_one(
            {"hire": self.hire, "step_id": step_id},
            {
                "$set": {**fields, "updated_at": self.clock},
                "$inc": {"run_count": runs},
                "$setOnInsert": {key: value for key, value in defaults.items() if key not in fields},
            },
            upsert=True,
        )

    def text(self, step_id: str) -> str:
        return EVIDENCE[step_id].format(hire=self.hire, issue=HIRE_ISSUES[self.hire])

    def terminal(self, step_id: str, command: str, lines: list[str]) -> None:
        for line in [f"$ {command}", *lines]:
            self.emit(EventType.TERMINAL_OUTPUT, step_id, command=command, chunk=line + "\n")
            self.wait(0.15)

    def check(self, step_id: str, ok: bool, evidence: str) -> None:
        self.emit(EventType.STEP_CHECKING, step_id)
        self.wait(0.4)
        self.emit(EventType.STEP_CHECK_RESULT, step_id, ok=ok, evidence=evidence)
        if not ok:
            self.set_step(step_id, check_ever_failed=True)

    def done(self, step_id: str, status: StepStatus, source: FixSource | None = None) -> None:
        evidence = self.text(step_id)
        llm_calls = 1 if source is FixSource.REASONING else 0
        self.set_step(step_id, status=status.value, evidence=evidence, fix_source=source.value if source else None, llm_calls=llm_calls)
        self.emit(EventType.STEP_STATUS, step_id, status=status.value, evidence=evidence, reconciled=False)
        self.wait(0.5)

    def step(self, step_id: str, fix: FixSource | None = None, hint: dict[str, Any] | None = None) -> None:
        """Check fails, intent written, run(); then either verified, or the check fails and a fix is applied."""
        self.check(step_id, False, "not done yet")
        self.set_step(step_id, status=StepStatus.PENDING.value, runs=1, idempotency_key=idempotency_key(self.hire, step_id))
        self.emit(EventType.STEP_PENDING, step_id)
        if step_id in LOCATORS:
            goal, locator = LOCATORS[step_id]
            self.emit(EventType.LOCATOR, step_id, goal=goal, mode="memory" if self.hire != "ada" else "reasoning", locator=locator)
            self.wait(1.2)
        else:
            command = RUN_COMMANDS[step_id].format(key=idempotency_key(self.hire, step_id))
            self.terminal(step_id, command, ["…", "done"])
        if fix is None:
            self.check(step_id, True, self.text(step_id))
            self.done(step_id, StepStatus.VERIFIED)
            return
        self.repair(step_id, step_id, fix, hint)
        self.done(step_id, FIXED_STATUS[fix], fix)

    def repair(self, step_id: str, snag: str, fix: FixSource, hint: dict[str, Any] | None) -> None:
        self.check(step_id, False, ERRORS[snag].splitlines()[0])
        self.emit(EventType.ERROR_CAPTURED, step_id, fingerprint=f"fp-{snag}", error_text=ERRORS[snag])
        self.wait(0.6)
        commands = list((hint or {}).get("commands", FIXES[snag]))
        if fix is FixSource.REASONING:
            self.emit(EventType.MEMORY_MISS, step_id)
            self.emit(EventType.REASONING_STARTED, step_id)
            self.wait(2.5)
        else:
            self.emit(
                EventType.MEMORY_HIT,
                step_id,
                **{"commands": commands, "source": fix.value, "rationale": "same error seen before", "experience_id": f"exp-{snag}",
                   "similarity": None, "source_hire": "ada", "adapted_from": None, **(hint or {})},
            )
            self.wait(0.4)
        for command in commands:
            self.terminal(step_id, command, ["…", "ok"])
        self.emit(EventType.FIX_ATTEMPT, step_id, source=fix.value, commands=commands, ok=True)
        self.check(step_id, True, self.text(step_id))
        self.emit(EventType.EXPERIENCE_SAVED, step_id, fingerprint=f"fp-{snag}")


FIXED_STATUS = {
    FixSource.EXACT_MEMORY: StepStatus.FIXED_FROM_MEMORY,
    FixSource.SEMANTIC_MEMORY: StepStatus.FIXED_FROM_MEMORY,
    FixSource.REASONING: StepStatus.FIXED_BY_REASONING,
}
HIRE_ISSUES = {"ada": 1, "grace": 2, "linus": 3, "barbara": 4, "ken": 5}
SNAGS = ("t_runtime", "t_system_package", "t_settings", "t_init_db")
STEPS_BEFORE_KILL = ("t_base_tools", "t_ssh_keygen", "b_sign_in", "b_add_ssh_key", "b_open_welcome", "t_clone", "t_runtime", "t_system_package")
STEPS_AFTER_DRIFT = ("t_settings", "t_init_db", "t_run_tests", "b_checklist")


def start(db: Database, hire: str, machine: Machine, live: bool, speed: float, started_ago: timedelta = timedelta(0)) -> Ledger:
    db[queries.EVENTS].delete_many({"hire": hire})
    db[queries.STEPS].delete_many({"hire": hire})
    run_id = uuid.uuid4().hex
    db[queries.HIRES].replace_one(
        {"_id": hire}, {"_id": hire, "machine": machine.value, "container": container_name(hire), "run_id": run_id}, upsert=True
    )
    ledger = Ledger(db, hire, machine, run_id, live, speed, datetime.now(UTC) - started_ago)
    ledger.emit(EventType.RUN_STARTED, machine=machine.value)
    return ledger


def resumed(db: Database, hire: str, speed: float) -> Ledger:
    run_id = queries.latest_run_id(db, hire)
    if run_id is None:
        raise LookupError(f"no run for {hire} to resume; run the `barbara` scenario first")
    ledger = Ledger(db, hire, Machine(db[queries.HIRES].find_one({"_id": hire})["machine"]), run_id, True, speed)
    ledger.emit(EventType.RUN_RESUMED)
    return ledger


def recorded_hire(db: Database, hire: str, machine: Machine, fixes: dict[str, FixSource], ago: timedelta, pace: float) -> None:
    ledger = start(db, hire, machine, live=False, speed=1, started_ago=ago)
    for step_id in ("t_base_tools", "t_ssh_keygen", "b_sign_in", "b_add_ssh_key", "b_open_welcome", "t_clone", *SNAGS, "t_install_deps", "t_run_tests", "b_checklist"):
        ledger.wait(pace)
        ledger.step(step_id, fixes.get(step_id))
    ledger.emit(EventType.RUN_FINISHED, ok=True)


def scenario_recorded(db: Database, speed: float) -> None:
    reasoning = {snag: FixSource.REASONING for snag in SNAGS}
    memory = {snag: FixSource.EXACT_MEMORY for snag in SNAGS}
    recorded_hire(db, "ada", Machine.UBUNTU, reasoning, timedelta(hours=3), pace=30)
    recorded_hire(db, "grace", Machine.DEBIAN, memory, timedelta(hours=2), pace=8)
    recorded_hire(db, "linus", Machine.UBUNTU, memory, timedelta(hours=1), pace=6)


@contextmanager
def engine_pid(hire: str, killed: bool) -> Iterator[None]:
    """Look like an engine process to the page: record our pid; a simulated kill leaves the pid file behind."""
    processes.record_pid(hire)
    yield
    if not killed:
        processes.clear_pid(hire)


def scenario_barbara(db: Database, speed: float) -> None:
    with engine_pid("barbara", killed=True):
        ledger = start(db, "barbara", Machine.UBUNTU, live=True, speed=speed)
        for step_id in STEPS_BEFORE_KILL:
            ledger.step(step_id, FixSource.EXACT_MEMORY if step_id in SNAGS else None)
        ledger.check("t_install_deps", False, "not done yet")
        ledger.set_step("t_install_deps", status=StepStatus.PENDING.value, runs=1)
        ledger.emit(EventType.STEP_PENDING, "t_install_deps")
        ledger.terminal("t_install_deps", RUN_COMMANDS["t_install_deps"], ["Collecting fastapi", "Collecting psycopg2==2.9.10"])


def scenario_resume(db: Database, speed: float) -> None:
    with engine_pid("barbara", killed=False):
        ledger = resumed(db, "barbara", speed)
        for doc in queries.steps(db, "barbara"):
            if doc["status"] in DONE_STATUSES:
                # Like the engine: settled by its check, nothing run, the ledger keeps its status.
                ledger.check(doc["step_id"], True, doc["evidence"])
                ledger.emit(EventType.STEP_STATUS, doc["step_id"], status=doc["status"], evidence=doc["evidence"], reconciled=True)
                ledger.wait(0.3)
        ledger.check("t_install_deps", True, EVIDENCE["t_install_deps"])
        ledger.done("t_install_deps", StepStatus.VERIFIED)
        ledger.wait(2)
        ledger.emit(EventType.DRIFT_DETECTED, "t_clone", **{"from": HEAD_BEFORE, "to": HEAD_AFTER})
        reason = f"acme/acme-api@main moved {HEAD_BEFORE} -> {HEAD_AFTER}"
        for step_id in ("t_clone", "t_runtime", "t_system_package", "t_install_deps"):
            ledger.set_step(step_id, status=StepStatus.STALE.value, evidence=reason)
            ledger.emit(EventType.STEP_STATUS, step_id, status=StepStatus.STALE.value, evidence=reason, reconciled=False)
        ledger.wait(1)
        ledger.set_step("t_clone", status=StepStatus.PENDING.value, runs=1)
        ledger.terminal("t_clone", "git pull --ff-only", [f"Updating {HEAD_BEFORE}..{HEAD_AFTER}", " requirements.txt | 1 +"])
        evidence = f"local HEAD {HEAD_AFTER} = API HEAD {HEAD_AFTER}"
        ledger.check("t_clone", True, evidence)
        ledger.set_step("t_clone", status=StepStatus.VERIFIED.value, evidence=evidence)
        ledger.emit(EventType.STEP_STATUS, "t_clone", status=StepStatus.VERIFIED.value, evidence=evidence, reconciled=False)
        for step_id in ("t_runtime", "t_system_package"):
            ledger.check(step_id, True, EVIDENCE[step_id])
            ledger.set_step(step_id, status=StepStatus.SKIPPED.value, evidence=EVIDENCE[step_id])
            ledger.emit(EventType.STEP_STATUS, step_id, status=StepStatus.SKIPPED.value, evidence=EVIDENCE[step_id], reconciled=True)
        ledger.set_step("t_install_deps", status=StepStatus.PENDING.value, runs=1)
        ledger.emit(EventType.STEP_PENDING, "t_install_deps")
        ledger.terminal("t_install_deps", RUN_COMMANDS["t_install_deps"], ["Collecting lxml==5.3.0", "error: subprocess-exited-with-error"])
        ledger.repair("t_install_deps", "t_drift", FixSource.REASONING, None)
        ledger.done("t_install_deps", StepStatus.FIXED_BY_REASONING, FixSource.REASONING)
        for step_id in STEPS_AFTER_DRIFT:
            ledger.step(step_id, FixSource.EXACT_MEMORY if step_id in SNAGS else None)
        ledger.emit(EventType.README_ISSUE_OPENED, url="http://localhost:3000/acme/acme-api/issues/6", title="README: document libpq-dev")
        ledger.emit(EventType.RUN_FINISHED, ok=True)


def scenario_ken(db: Database, speed: float) -> None:
    apk = {"commands": ["apk add postgresql-dev gcc musl-dev python3-dev"], "similarity": 0.85, "source_hire": "ada", "adapted_from": "apt"}
    with engine_pid("ken", killed=False):
        ledger = start(db, "ken", Machine.ALPINE, live=True, speed=speed)
        for step_id in ("t_base_tools", "t_ssh_keygen", "b_sign_in", "b_add_ssh_key", "b_open_welcome", "t_clone", "t_runtime"):
            ledger.step(step_id)
        ledger.step("t_system_package", FixSource.SEMANTIC_MEMORY, apk)
        for step_id in ("t_install_deps", "t_settings", "t_init_db", "t_run_tests", "b_checklist"):
            ledger.step(step_id, FixSource.EXACT_MEMORY if step_id in SNAGS else None)
        ledger.emit(EventType.RUN_FINISHED, ok=True)


SCENARIOS = {"recorded": scenario_recorded, "barbara": scenario_barbara, "resume": scenario_resume, "ken": scenario_ken}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--speed", type=float, default=1.0, help="2 runs twice as fast")
    args = parser.parse_args()
    config = settings()
    if config.mongodb_db == INTEGRATION_DB:
        raise SystemExit(f"refusing to write stub events into the integration database {INTEGRATION_DB!r}")
    client, db = queries.connect(config)
    try:
        SCENARIOS[args.scenario](db, args.speed)
    finally:
        client.close()


if __name__ == "__main__":
    main()
