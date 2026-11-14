from pathlib import Path

import pytest

from dayone.contracts import ExecResult
from dayone.terminal import docker
from dayone.terminal.docker import ERROR_LINE_LIMIT, ERROR_TAIL_LINES, error_text


def test_command_and_workdir_come_first_then_the_output_tail():
    output = "".join(f"line {number}\n" for number in range(ERROR_TAIL_LINES + 5))
    text = error_text(ExecResult(".venv/bin/python -c 'x'", 1, output, 0.1), "/root/acme-api", "why it failed")
    lines = text.splitlines()
    print(text)
    assert lines[0] == "$ cd /root/acme-api && .venv/bin/python -c 'x'"
    assert lines[1] == "line 5" and lines[-2] == f"line {ERROR_TAIL_LINES + 4}"
    assert lines[-1] == "why it failed"


def test_without_workdir_the_command_runs_where_docker_exec_starts():
    assert error_text(ExecResult("ssh -T git@gitea", 255, "Permission denied (publickey).\n", 0.1)) == (
        "$ ssh -T git@gitea\nPermission denied (publickey)."
    )


ERRORS = Path(__file__).parent / "errors"
SHORT_TAIL = 5  # makes the real logs long relative to the tail, as QA's push_change run was


@pytest.mark.parametrize(
    ("fixture", "cause"),
    [
        ("debian_t_system_package_ldap.full.txt", "Modules/common.h:15:10: fatal error: lber.h: No such file or directory"),
        ("ubuntu_t_system_package_ldap.full.txt", "Modules/common.h:15:10: fatal error: lber.h: No such file or directory"),
        ("alpine_t_system_package.txt", "Error: pg_config executable not found."),
    ],
)
def test_the_cause_survives_when_the_tail_cuts_it_off(monkeypatch, fixture, cause):
    monkeypatch.setattr(docker, "ERROR_TAIL_LINES", SHORT_TAIL)
    output = (ERRORS / fixture).read_text()
    assert cause not in "\n".join(output.splitlines()[-SHORT_TAIL:]), "fixture no longer exercises the cut-off"
    text = error_text(ExecResult("pip wheel -r requirements.txt", 1, output, 0.1), "/root/acme-api")
    print(text)
    lines = text.splitlines()
    assert lines[0] == "$ cd /root/acme-api && pip wheel -r requirements.txt"
    assert cause in [line.strip() for line in lines]
    assert lines[-SHORT_TAIL:] == output.strip().splitlines()[-SHORT_TAIL:]
    assert len(lines) == len(set(lines)), "error lines are listed once and not repeated from the tail"


def test_error_lines_before_the_tail_are_capped():
    noisy = "".join(f"error: problem {number}\n" for number in range(ERROR_LINE_LIMIT * 2))
    output = noisy + "".join(f"progress {number}\n" for number in range(ERROR_TAIL_LINES))
    lines = error_text(ExecResult("make", 2, output, 0.1)).splitlines()
    assert lines[1 : ERROR_LINE_LIMIT + 1] == [f"error: problem {number}" for number in range(ERROR_LINE_LIMIT)]
    assert len(lines) == 1 + ERROR_LINE_LIMIT + ERROR_TAIL_LINES
