"""Fingerprints group the same error across machines and keep different errors apart.

The fixtures in errors/ are real error_text captured by test_steps_live.py from fresh containers.
"""

from pathlib import Path

import pytest

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
