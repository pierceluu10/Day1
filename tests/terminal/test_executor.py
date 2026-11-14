import subprocess

import pytest

from dayone.contracts import EventType, Machine

UNIT_CONTAINER = "dayone-dev3-unit"  # separate from the live-test laptop so the two never disturb each other
COMMAND_TIMEOUT_S = 10.0


def _container_id(name: str) -> str:
    return subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name], capture_output=True, text=True).stdout.strip()


@pytest.fixture(params=[Machine.UBUNTU, Machine.ALPINE])
def executor(request, make_executor):
    executor = make_executor(request.param, UNIT_CONTAINER)
    executor.remove_container()
    executor.ensure_container()
    yield executor
    executor.remove_container()


def test_exec_streams_output_and_returns_exit_code(executor):
    result = executor.exec("echo out; echo err >&2; exit 3", COMMAND_TIMEOUT_S)
    print(result)
    assert result.exit_code == 3 and not result.ok and not result.timed_out
    assert "out\n" in result.output and "err\n" in result.output
    chunks = [data["chunk"] for type, _, data in executor.events.events if type == EventType.TERMINAL_OUTPUT]
    assert "".join(chunks) == result.output


def test_exec_runs_in_workdir_with_noninteractive_env(executor):
    result = executor.exec('pwd; echo "$DEBIAN_FRONTEND"', COMMAND_TIMEOUT_S, workdir="/tmp")
    assert result.ok and result.output.split() == ["/tmp", "noninteractive"]


def test_timeout_kills_the_whole_process_tree(executor):
    result = executor.exec("sleep 300 & sleep 300; echo never", 2)
    assert result.timed_out and not result.ok
    assert "never" not in result.output
    survivors = executor.exec("cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' ' '", COMMAND_TIMEOUT_S).output
    print(survivors)
    assert "sleep 300" not in survivors


def test_ensure_container_reuses_the_running_laptop(executor):
    executor.exec("touch /root/marker", COMMAND_TIMEOUT_S)
    first = _container_id(UNIT_CONTAINER)
    subprocess.run(["docker", "stop", "-t", "0", UNIT_CONTAINER], capture_output=True, check=True)
    executor.ensure_container()
    executor.ensure_container()
    assert _container_id(UNIT_CONTAINER) == first
    assert executor.exec("test -f /root/marker", COMMAND_TIMEOUT_S).ok


def test_facts_on_an_empty_machine(executor):
    facts = executor.facts()
    print(facts)
    expected = {Machine.UBUNTU: ("ubuntu", "22.04", "apt"), Machine.ALPINE: ("alpine", "3.20", "apk")}
    family, version, package_manager = expected[executor.hire.machine]
    assert (facts.os_family, facts.package_manager) == (family, package_manager)
    assert facts.os_version.startswith(version)
    assert "git" not in facts.tools and "ssh" not in facts.tools
