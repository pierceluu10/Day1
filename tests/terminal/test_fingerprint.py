"""Fingerprints group the same error across machines and keep different errors apart.

The fixtures in errors/ are real error_text captured by test_steps_live.py from fresh containers.
"""

from pathlib import Path

import pytest

from dayone.contracts import REPO_DIR, ExecResult, Hire, Machine, StepContext
from dayone.steps.terminal_steps import VENV_PYTHON, check_init_db, check_settings, check_system_package
from dayone.terminal.fingerprint import fingerprint, normalise

ERRORS = Path(__file__).parent / "errors"


def _error(name: str) -> str:
    return (ERRORS / name).read_text()


@pytest.mark.parametrize("step", ["t_system_package", "t_settings", "t_init_db"])
def test_same_snag_on_ubuntu_and_alpine_matches(step):
    ubuntu, alpine = _error(f"ubuntu_{step}.txt"), _error(f"alpine_{step}.txt")
    print(f"--- ubuntu ---\n{normalise(ubuntu)}\n--- alpine ---\n{normalise(alpine)}")
    assert fingerprint(ubuntu) == fingerprint(alpine)


def test_pip_noise_differs_but_the_cause_matches():
    ubuntu, alpine = _error("ubuntu_t_system_package.txt"), _error("alpine_t_system_package.txt")
    assert ubuntu != alpine  # different pip versions print different wrappers, warnings and progress
    assert normalise(ubuntu) == normalise(alpine) == "Error: pg_config executable not found."


def test_missing_header_matches_across_compilers_and_differs_from_pg_config():
    debian, ubuntu = _error("debian_t_system_package_ldap.full.txt"), _error("ubuntu_t_system_package_ldap.full.txt")
    print(normalise(debian))
    assert normalise(debian) == normalise(ubuntu) == "Modules<path>:<n>:<n>: fatal error: lber.h: No such file or directory"
    assert fingerprint(debian) != fingerprint(_error("debian_t_system_package.txt"))


def test_different_snags_do_not_collide():
    names = ["ubuntu_t_runtime.txt", "ubuntu_t_system_package.txt", "ubuntu_t_settings.txt", "ubuntu_t_init_db.txt"]
    fingerprints = {fingerprint(_error(name)) for name in names}
    assert len(fingerprints) == len(names)


def test_paths_versions_hashes_and_times_are_normalised():
    first = (
        "2026-11-14T13:02:11Z fatal: could not read /tmp/pip-build-env-k2j3h/overlay/lib/python3.10/x.py line 41\n"
        "ERROR: Package 'acme-api' requires a different Python: 3.10.12 not in '>=3.12' (commit 3f2a1c9e, 385 kB)"
    )
    second = (
        "2026-11-15T09:44:52.123+01:00 fatal: could not read /var/tmp/pip-build-env-zz81q/overlay/lib/python3.11/x.py"
        " line 7\n"
        "ERROR: Package 'acme-api' requires a different Python: 3.11.2 not in '>=3.12' (commit 8be04d1a, 1.2 MB)"
    )
    print(normalise(first))
    assert normalise(first) == normalise(second)
    assert fingerprint(first) == fingerprint(second)


def test_the_error_message_itself_still_distinguishes():
    assert fingerprint("sqlite3.OperationalError: no such table: users") != fingerprint(
        "sqlite3.OperationalError: no such table: orders"
    )
    assert fingerprint("Error: pg_config executable not found.") != fingerprint(
        "error: command 'gcc' failed: No such file or directory"
    )


def test_text_without_error_lines_is_fingerprinted_whole():
    assert normalise("ssh: connect to host gitea port 22: Connection refused") == (
        "ssh: connect to host gitea port <n>: Connection refused"
    )


class ReplayExecutor:
    """Answers every command with one captured output, so a real check builds its error_text from it."""

    def __init__(self, output: str) -> None:
        self.output = output

    def exec(self, command, timeout_s, workdir=None):
        return ExecResult(command, 1, self.output, 0.1)


@pytest.mark.parametrize(
    ("step", "check"),
    [("t_system_package", check_system_package), ("t_settings", check_settings), ("t_init_db", check_init_db)],
)
def test_check_error_text_leads_with_the_command_and_still_matches_across_machines(step, check):
    def failing(machine):
        executor = ReplayExecutor(_error(f"{machine}_{step}.txt"))
        ctx = StepContext(Hire("grace", machine, "dayone-dev3", "r"), step, "k", executor, None, None, None, None)
        return check(ctx)

    ubuntu, alpine = failing(Machine.UBUNTU), failing(Machine.ALPINE)
    print(ubuntu.error_text.splitlines()[0])
    assert not ubuntu.ok
    command = ubuntu.error_text.splitlines()[0]
    assert command.startswith(f"$ cd {REPO_DIR} && ") and f"{VENV_PYTHON} " in command
    assert fingerprint(ubuntu.error_text) == fingerprint(alpine.error_text)
