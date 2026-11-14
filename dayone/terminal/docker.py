"""The hire's laptop: one long-lived Docker container driven through the `docker` CLI."""

from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid

from dayone.contracts import DOCKER_NETWORK, MACHINE_IMAGES, EventBus, EventType, ExecResult, Hire, MachineFacts

KEEP_ALIVE = ("tail", "-f", "/dev/null")  # works with coreutils and busybox alike
EXEC_ENV = ("DEBIAN_FRONTEND=noninteractive", "PIP_DISABLE_PIP_VERSION_CHECK=1", "PIP_NO_INPUT=1")
EXIT_TIMED_OUT = 124  # same code coreutils `timeout` uses
BATCH_LINES = 20
BATCH_INTERVAL_S = 0.5
KILL_TIMEOUT_S = 10.0
FACT_TOOLS = {
    "python": "python3 --version",
    "git": "git --version",
    "pip": "python3 -m pip --version",
    "ssh": "ssh -V",
}
VERSION = re.compile(r"\d+(?:\.\d+)+\w*")
PACKAGE_MANAGERS = {"alpine": "apk", "ubuntu": "apt", "debian": "apt"}
FACT_MARKER = "@@dayone-fact "


class DockerError(RuntimeError):
    """The docker CLI itself failed (not a command inside the container)."""


def _docker(*args: str, timeout_s: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout_s, check=False)


def _parse_os_release(text: str) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {key: value.strip().strip('"') for key, value in pairs}


class DockerExecutor:
    def __init__(self, hire: Hire, events: EventBus, timeout_s: float) -> None:
        self.hire = hire
        self.events = events
        self.timeout_s = timeout_s  # bounds the executor's own docker calls, not step commands
        self.container = hire.container

    def _state(self) -> str | None:
        """'running', 'exited', ... or None when the container does not exist."""
        result = _docker("inspect", "-f", "{{.State.Status}}", self.container, timeout_s=self.timeout_s)
        return result.stdout.strip() if result.returncode == 0 else None

    def ensure_container(self) -> None:
        state = self._state()
        if state == "running":
            return
        if state is None:
            args = ("run", "-d", "--init", "--name", self.container, "--hostname", self.container)
            image = MACHINE_IMAGES[self.hire.machine]
            result = _docker(*args, "--network", DOCKER_NETWORK, image, *KEEP_ALIVE, timeout_s=self.timeout_s)
        else:
            result = _docker("start", self.container, timeout_s=self.timeout_s)
        if result.returncode != 0:
            raise DockerError(f"cannot start {self.container}: {result.stderr.strip()}")

    def remove_container(self) -> None:
        if self._state() is None:
            return
        result = _docker("rm", "-f", self.container, timeout_s=self.timeout_s)
        if result.returncode != 0:
            raise DockerError(f"cannot remove {self.container}: {result.stderr.strip()}")

    def exec(self, command: str, timeout_s: float, workdir: str | None = None) -> ExecResult:
        pidfile = f"/tmp/dayone-exec-{uuid.uuid4().hex}.pid"
        # The exec'd shell leads its own process group; recording its pid lets a timeout kill the whole tree.
        wrapper = f'echo $$ > {pidfile}; sh -lc "$0"; status=$?; rm -f {pidfile}; exit $status'
        args = ["docker", "exec"]
        for variable in EXEC_ENV:
            args += ["-e", variable]
        if workdir:
            args += ["-w", workdir]
        args += [self.container, "sh", "-c", wrapper, command]

        started = time.monotonic()
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines: list[str] = []
        reader = threading.Thread(target=self._stream, args=(process, command, lines), daemon=True)
        reader.start()
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(pidfile)
            process.kill()
            process.wait()
            exit_code = EXIT_TIMED_OUT
        reader.join()
        output = "".join(lines)
        if timed_out:
            output += f"\n[dayone] command timed out after {timeout_s:.0f}s and was killed\n"
        return ExecResult(command, exit_code, output, time.monotonic() - started, timed_out)

    def _stream(self, process: subprocess.Popen[str], command: str, lines: list[str]) -> None:
        assert process.stdout is not None
        batch: list[str] = []
        last_emit = time.monotonic()
        with process.stdout:
            for line in process.stdout:
                lines.append(line)
                batch.append(line)
                if len(batch) >= BATCH_LINES or time.monotonic() - last_emit >= BATCH_INTERVAL_S:
                    self.events.emit(EventType.TERMINAL_OUTPUT, command=command, chunk="".join(batch))
                    batch.clear()
                    last_emit = time.monotonic()
        if batch:
            self.events.emit(EventType.TERMINAL_OUTPUT, command=command, chunk="".join(batch))

    def _kill_group(self, pidfile: str) -> None:
        # dash (Ubuntu/Debian sh) rejects `kill -- -pid`; `kill -9 -pid` works in dash and busybox ash.
        kill = f'pid=$(cat {pidfile}) && kill -9 -$pid; rm -f {pidfile}'
        _docker("exec", self.container, "sh", "-c", kill, timeout_s=KILL_TIMEOUT_S)

    def facts(self) -> MachineFacts:
        probes = "; ".join(f"echo '{FACT_MARKER}{name}'; {probe} 2>&1" for name, probe in FACT_TOOLS.items())
        result = _docker("exec", self.container, "sh", "-c", f"cat /etc/os-release; {probes}", timeout_s=self.timeout_s)
        if result.returncode != 0 and FACT_MARKER not in result.stdout:
            raise DockerError(f"cannot read facts from {self.container}: {result.stderr.strip()}")
        os_release, *sections = result.stdout.split(FACT_MARKER)
        release = _parse_os_release(os_release)
        tools = {}
        for section in sections:
            name, _, output = section.partition("\n")
            version = VERSION.search(output)
            if version and "not found" not in output:
                tools[name] = version.group()
        os_family = release["ID"]
        return MachineFacts(os_family, release["VERSION_ID"], PACKAGE_MANAGERS[os_family], tools)
