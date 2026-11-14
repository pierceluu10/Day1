from dayone.contracts import ExecResult
from dayone.terminal.docker import ERROR_TAIL_LINES, error_text


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
