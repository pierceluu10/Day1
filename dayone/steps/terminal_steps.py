"""Terminal steps on the hire's laptop.

Each run() is the naive action the acme-api README implies (it mentions no snags). Each check() verifies the
real goal by another route, so the repo's snags surface as real failures whose error text memory then fixes.
"""

from __future__ import annotations

import operator
import re
import shlex
import tomllib
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable

from dayone.contracts import (
    GITEA_HOSTNAME,
    REPO,
    REPO_BRANCH,
    REPO_DIR,
    WELCOME_CHECKLIST,
    CheckResult,
    ExecResult,
    RunResult,
    Step,
    StepContext,
    StepKind,
)
from dayone.terminal.ssh import SSH_OPTIONS

QUICK_TIMEOUT_S = 60.0
INSTALL_TIMEOUT_S = 900.0
TEST_TIMEOUT_S = 300.0
ERROR_TAIL_LINES = 40
SHORT_SHA = 7

VENV_DIR = f"{REPO_DIR}/.venv"
VENV_PYTHON = f"{VENV_DIR}/bin/python"
KEY_PATH = "/root/.ssh/id_ed25519"
CLONE_URL = f"git@{GITEA_HOSTNAME}:{REPO}.git"
WHEEL_DIR = "/tmp/dayone-wheels"
JUNIT_PATH = "/tmp/dayone-junit.xml"

BASE_TOOLS = {
    "apt": "apt-get update && apt-get install -y --no-install-recommends "
    "git openssh-client curl ca-certificates python3 python3-venv python3-pip",
    "apk": "apk add --no-cache git openssh-client curl ca-certificates python3 py3-pip",
}
REQUIRED_TOOLS = ("git", "ssh", "python", "pip")
KEY_LINE = re.compile(r"^(?P<bits>\d+) (?P<fingerprint>SHA256:\S+) (?P<comment>.*) \((?P<type>\w+)\)$")
SPECIFIER = re.compile(r"(?P<op>>=|<=|==|!=|>|<)\s*(?P<version>\d+(?:\.\d+)*)")
COMPARISONS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, "!=": operator.ne, ">": operator.gt, "<": operator.lt}


class UnsupportedSpecifier(ValueError):
    pass


def _tail(output: str) -> str:
    return "\n".join(output.strip().splitlines()[-ERROR_TAIL_LINES:])


def _exec(ctx: StepContext, command: str, timeout_s: float = QUICK_TIMEOUT_S, workdir: str | None = None) -> ExecResult:
    return ctx.executor.exec(command, timeout_s, workdir)


def _failed(evidence: str, result: ExecResult, **detail: object) -> CheckResult:
    return CheckResult(False, evidence, {"exit_code": result.exit_code, **detail}, _tail(result.output))


def _shell_run(command: str, timeout_s: float = INSTALL_TIMEOUT_S, workdir: str | None = None) -> Callable[[StepContext], RunResult]:
    def run(ctx: StepContext) -> RunResult:
        result = _exec(ctx, command, timeout_s, workdir)
        return RunResult(result.ok, result.output, (command,))

    return run


def _readme_says_nothing(what: str) -> Callable[[StepContext], RunResult]:
    def run(ctx: StepContext) -> RunResult:
        return RunResult(True, f"the README's getting-started steps say nothing about {what}")

    return run


def satisfies(version: str, specifier: str) -> bool:
    """PEP 440 comparison for plain release versions, enough for requires-python."""
    have = tuple(int(part) for part in version.split("."))
    for clause in specifier.split(","):
        match = SPECIFIER.fullmatch(clause.strip())
        if match is None:
            raise UnsupportedSpecifier(f"cannot evaluate requires-python clause {clause.strip()!r}")
        want = tuple(int(part) for part in match["version"].split("."))
        width = max(len(have), len(want))
        padded_have, padded_want = (parts + (0,) * (width - len(parts)) for parts in (have, want))
        if not COMPARISONS[match["op"]](padded_have, padded_want):
            return False
    return True


# --- t_base_tools ---------------------------------------------------------------


def run_base_tools(ctx: StepContext) -> RunResult:
    command = BASE_TOOLS[ctx.executor.facts().package_manager]
    return _shell_run(command)(ctx)


def check_base_tools(ctx: StepContext) -> CheckResult:
    tools = ctx.executor.facts().tools
    probe = _exec(ctx, "curl --version && python3 -c 'import ensurepip, venv'")
    missing = [tool for tool in REQUIRED_TOOLS if tool not in tools]
    curl = probe.output.split()[1] if probe.ok else "missing"
    evidence = ", ".join(f"{tool} {tools.get(tool, 'missing')}" for tool in REQUIRED_TOOLS) + f", curl {curl}"
    if missing or not probe.ok:
        return _failed(evidence, probe, tools=tools, missing=missing)
    return CheckResult(True, evidence, {"tools": tools, "curl": curl})


# --- t_ssh_keygen ---------------------------------------------------------------


def check_ssh_keygen(ctx: StepContext) -> CheckResult:
    result = _exec(ctx, f"ssh-keygen -lf {KEY_PATH}.pub && cat {KEY_PATH}.pub")
    if not result.ok:
        return _failed("no ssh key", result)
    listing, public_key = result.output.strip().splitlines()[:2]
    match = KEY_LINE.match(listing)
    if match is None or match["type"] != "ED25519" or match["comment"] != ctx.idempotency_key:
        return _failed(f"unexpected key: {listing}", result)
    detail = {"fingerprint": match["fingerprint"], "public_key": public_key, "comment": match["comment"]}
    return CheckResult(True, f"ED25519 {match['fingerprint']}", detail)


def run_ssh_keygen(ctx: StepContext) -> RunResult:
    comment = shlex.quote(ctx.idempotency_key)
    command = (
        f"test -f {KEY_PATH} || (mkdir -p -m 700 /root/.ssh && ssh-keygen -q -t ed25519 -N '' -C {comment} -f {KEY_PATH})"
    )
    return _shell_run(command, QUICK_TIMEOUT_S)(ctx)


# --- t_clone --------------------------------------------------------------------

CLONE_OR_FAST_FORWARD = (
    f'export GIT_SSH_COMMAND="ssh {SSH_OPTIONS}"; '
    f"if [ -d {REPO_DIR}/.git ]; then "
    f"git -C {REPO_DIR} fetch origin {REPO_BRANCH} && git -C {REPO_DIR} merge --ff-only FETCH_HEAD; "
    f"else git clone --branch {REPO_BRANCH} {CLONE_URL} {REPO_DIR}; fi"
)


def check_clone(ctx: StepContext) -> CheckResult:
    api_head = ctx.gitea.branch_head(REPO, REPO_BRANCH)
    result = _exec(ctx, f"git -C {REPO_DIR} rev-parse HEAD")
    if not result.ok:
        return _failed(f"no clone at {REPO_DIR}; API HEAD {api_head[:SHORT_SHA]}", result, head=api_head)
    local_head = result.output.strip()
    detail = {"head": api_head, "local_head": local_head}
    if local_head != api_head:
        evidence = f"local HEAD {local_head[:SHORT_SHA]} ≠ API HEAD {api_head[:SHORT_SHA]}"
        error = f"{REPO_DIR} is at {local_head} but {REPO} {REPO_BRANCH} is at {api_head}"
        return CheckResult(False, evidence, detail, error)
    return CheckResult(True, f"local HEAD {local_head[:SHORT_SHA]} = API HEAD {api_head[:SHORT_SHA]}", detail)


# --- t_runtime ------------------------------------------------------------------


def check_runtime(ctx: StepContext) -> CheckResult:
    pyproject = _exec(ctx, f"cat {REPO_DIR}/pyproject.toml")
    if not pyproject.ok:
        return _failed("no pyproject.toml", pyproject)
    required = tomllib.loads(pyproject.output)["project"]["requires-python"]
    probe = _exec(ctx, f"{VENV_PYTHON} -c 'import platform; print(platform.python_version())'")
    if not probe.ok:
        return _failed(f"no venv python at {VENV_PYTHON}; requires-python {required}", probe, requires_python=required)
    version = probe.output.strip()
    detail = {"python": version, "requires_python": required}
    if not satisfies(version, required):
        error = f"{VENV_PYTHON} is Python {version}, but {REPO_DIR}/pyproject.toml requires-python = '{required}'"
        return CheckResult(False, f"venv Python {version} does not satisfy {required}", detail, error)
    return CheckResult(True, f"venv Python {version} satisfies {required}", detail)


# --- t_system_package -------------------------------------------------------------


def check_system_package(ctx: StepContext) -> CheckResult:
    # With dependencies: requirements.txt is only `-e .`, so the native deps live in pyproject's dependencies.
    command = f"rm -rf {WHEEL_DIR} && {VENV_PYTHON} -m pip wheel -r requirements.txt -w {WHEEL_DIR}"
    result = _exec(ctx, command, INSTALL_TIMEOUT_S, REPO_DIR)
    if not result.ok:
        return _failed("native dependencies do not build", result)
    wheels = _exec(ctx, f"ls {WHEEL_DIR}").output.split()
    return CheckResult(True, f"{len(wheels)} wheels built, native deps compile", {"wheels": wheels})


# --- t_install_deps ----------------------------------------------------------------


def check_install_deps(ctx: StepContext) -> CheckResult:
    command = (
        f"{VENV_PYTHON} -m pip check && "
        f"{VENV_PYTHON} -c 'import acme_api.app, psycopg2; print(psycopg2.__version__.split()[0])' && "
        f"{VENV_PYTHON} -m pip list --format=freeze | wc -l"
    )
    result = _exec(ctx, command, QUICK_TIMEOUT_S, REPO_DIR)
    if not result.ok:
        return _failed("dependencies not importable", result)
    *_, psycopg2_version, count = result.output.split()
    detail = {"psycopg2": psycopg2_version, "packages": int(count)}
    return CheckResult(True, f"pip check ok, {count} packages, psycopg2 {psycopg2_version}", detail)


# --- t_settings ---------------------------------------------------------------------


def check_settings(ctx: StepContext) -> CheckResult:
    command = f"{VENV_PYTHON} -c 'from acme_api.settings import settings; print(settings().database_url)'"
    result = _exec(ctx, command, QUICK_TIMEOUT_S, REPO_DIR)
    if not result.ok:
        return _failed("settings do not load", result)
    url = result.output.strip()
    return CheckResult(True, f"settings load, database {url}", {"database_url": url})


# --- t_init_db ----------------------------------------------------------------------

COUNT_USERS = """from acme_api import db
with db.connection() as conn:
    print(db.execute(conn, "SELECT count(*) FROM users").fetchone()[0])"""


def check_init_db(ctx: StepContext) -> CheckResult:
    result = _exec(ctx, f"{VENV_PYTHON} -c {shlex.quote(COUNT_USERS)}", QUICK_TIMEOUT_S, REPO_DIR)
    if not result.ok:
        return _failed("database schema missing", result)
    rows = int(result.output.split()[-1])
    return CheckResult(True, f"users table present ({rows} rows)", {"users": rows})


# --- t_run_tests ---------------------------------------------------------------------


def check_run_tests(ctx: StepContext) -> CheckResult:
    command = f"rm -f {JUNIT_PATH}; {VENV_PYTHON} -m pytest -q -p no:cacheprovider --junitxml={JUNIT_PATH}"
    result = _exec(ctx, command, TEST_TIMEOUT_S, REPO_DIR)
    report = _exec(ctx, f"cat {JUNIT_PATH}")
    if not report.ok:
        return _failed(f"pytest wrote no report, exit {result.exit_code}", result)
    suite = ElementTree.fromstring(report.output)
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    collected = int(suite.get("tests", 0))
    unsuccessful = sum(int(suite.get(field, 0)) for field in ("failures", "errors", "skipped"))
    passed = collected - unsuccessful
    detail = {"collected": collected, "passed": passed, "exit_code": result.exit_code}
    evidence = f"{passed} passed of {collected}, exit {result.exit_code}"
    if result.exit_code != 0 or collected == 0 or passed != collected:
        return CheckResult(False, evidence, detail, _tail(result.output))
    return CheckResult(True, f"{passed} passed, exit 0", detail)


def _step(step_id: str, title: str, check: Callable[[StepContext], CheckResult], run: Callable[[StepContext], RunResult]) -> Step:
    return Step(step_id, StepKind.TERMINAL, title, check, run, WELCOME_CHECKLIST.get(step_id))


TERMINAL_STEPS: dict[str, Step] = {
    step.id: step
    for step in (
        _step("t_base_tools", "Install base tools", check_base_tools, run_base_tools),
        _step("t_ssh_keygen", "Generate an SSH key", check_ssh_keygen, run_ssh_keygen),
        _step("t_clone", "Clone acme/acme-api over SSH", check_clone, _shell_run(CLONE_OR_FAST_FORWARD)),
        _step(
            "t_runtime",
            "Create the Python virtualenv",
            check_runtime,
            _shell_run(f"test -x {VENV_PYTHON} || python3 -m venv {VENV_DIR}", workdir=REPO_DIR),
        ),
        _step("t_system_package", "Install system packages", check_system_package, _readme_says_nothing("system packages")),
        _step(
            "t_install_deps",
            "Install the project dependencies",
            check_install_deps,
            _shell_run(f"{VENV_PYTHON} -m pip install -r requirements.txt", workdir=REPO_DIR),
        ),
        _step("t_settings", "Configure local settings", check_settings, _readme_says_nothing("a settings file")),
        _step("t_init_db", "Initialise the local database", check_init_db, _readme_says_nothing("the database")),
        _step(
            "t_run_tests",
            "Run the test suite",
            check_run_tests,
            _shell_run(f"{VENV_PYTHON} -m pytest -q", TEST_TIMEOUT_S, REPO_DIR),
        ),
    )
}
