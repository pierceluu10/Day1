"""t_run_tests's check reads the report recorded for the current HEAD, so its branches are tested offline."""

from dayone.contracts import ExecResult, Hire, Machine, StepContext
from dayone.steps.terminal_steps import REPORT_MARKER, check_run_tests

HEAD = "8094472c0ffee0000000000000000000000000aa"
FAILING_REPORT = (
    '<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="13" failures="0" errors="10" skipped="0">'
    '<testcase classname="tests.test_users" name="test_create"><error message="sqlite3.OperationalError: no such'
    ' table: users"/></testcase></testsuite></testsuites>'
)
PASSING_REPORT = '<testsuites><testsuite name="pytest" tests="13" failures="0" errors="0" skipped="0"/></testsuites>'


class ScriptedExecutor:
    """HEAD for rev-parse; the recorded report (or its absence) for everything else."""

    def __init__(self, report: tuple[int, str] | None) -> None:
        self.report = report

    def exec(self, command, timeout_s, workdir=None):
        if "rev-parse" in command:
            return ExecResult(command, 0, HEAD + "\n", 0.1)
        if self.report is None:
            return ExecResult(command, 1, f"cat: /root/.dayone/tests-{HEAD}.exit: No such file or directory\n", 0.1)
        exit_code, xml = self.report
        return ExecResult(command, 0, f"{exit_code}\n{REPORT_MARKER}\n{xml}\n", 0.1)


def _check(report):
    executor = ScriptedExecutor(report)
    hire = Hire("grace", Machine.UBUNTU, "dayone-dev3", "r")
    return check_run_tests(StepContext(hire, "t_run_tests", "k", executor, None, None, None, None))


def test_fresh_hire_fails_until_the_suite_ran_on_this_head():
    result = _check(None)
    print(result)
    assert not result.ok and result.evidence == "tests not run at 8094472"
    assert f"no test run recorded for HEAD {HEAD}" in result.error_text


def test_recorded_failures_surface_with_their_messages():
    result = _check((1, FAILING_REPORT))
    print(result.error_text)
    assert not result.ok and result.evidence == "3 passed of 13, exit 1 at 8094472"
    assert result.error_text.startswith("$ cd /root/acme-api && /root/acme-api/.venv/bin/python -m pytest -q")
    assert "tests.test_users.test_create: sqlite3.OperationalError: no such table: users" in result.error_text


def test_recorded_green_run_on_this_head_verifies():
    result = _check((0, PASSING_REPORT))
    assert result.ok and result.evidence == "13 passed of 13, exit 0 at 8094472"
    assert result.detail == {"collected": 13, "passed": 13, "exit_code": 0, "head": HEAD}
