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
ERROR_TAIL_LINES = 40
ERROR_LINE_LIMIT = 20
# Compiler and pip error lines, wherever they sit in the line ("Modules/common.h:15:10: fatal error: lber.h: ...").
ERROR_LINE = re.compile(r"\b(?:fatal error|error):", re.I)
PIDFILE_PREFIX = "/tmp/dayone-exec-"
ORPHAN_PIDFILES = f"{PIDFILE_PREFIX}*.pid"


def error_text(result: ExecResult, workdir: str | None = None, message: str = "") -> str:
    """What a failing check hands memory: the exact command first (fixes must reproduce its context), then the
    error lines from anywhere in the output (long build logs push the cause out of the tail), then the tail."""
    command = f"$ cd {workdir} && {result.command}" if workdir else f"$ {result.command}"
    lines = result.output.strip().splitlines()
    tail = lines[-ERROR_TAIL_LINES:]
    earlier: list[str] = []
    for line in lines[: len(lines) - len(tail)]:
        if ERROR_LINE.search(line) and line not in earlier and line not in tail:
            earlier.append(line)
    output = "\n".join([*earlier[:ERROR_LINE_LIMIT], *tail])
    return "\n".join(part for part in (command, output, message) if part)


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
        if state != "running":
            if state is None:
                args = ("run", "-d", "--init", "--name", self.container, "--hostname", self.container)
                image = MACHINE_IMAGES[self.hire.machine]
                result = _docker(*args, "--network", DOCKER_NETWORK, image, *KEEP_ALIVE, timeout_s=self.timeout_s)
            else:
                result = _docker("start", self.container, timeout_s=self.timeout_s)
            if result.returncode != 0:
                raise DockerError(f"cannot start {self.container}: {result.stderr.strip()}")
        # The lease allows one run per hire, so a pidfile left behind belongs to a run that was killed, and its
        # command may still hold locks (dpkg) the resumed run needs.
        self._kill_group(ORPHAN_PIDFILES)

    def remove_container(self) -> None:
        if self._state() is None:
            return
        result = _docker("rm", "-f", self.container, timeout_s=self.timeout_s)
        if result.returncode != 0:
            raise DockerError(f"cannot remove {self.container}: {result.stderr.strip()}")

    def exec(self, command: str, timeout_s: float, workdir: str | None = None) -> ExecResult:
        pidfile = f"{PIDFILE_PREFIX}{uuid.uuid4().hex}.pid"
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

    def _kill_group(self, pidfiles: str) -> None:
        """Kill the process group recorded in each pidfile matching `pidfiles` (a path or a glob), then remove it."""
        # dash (Ubuntu/Debian sh) rejects `kill -- -pid`; `kill -9 -pid` works in dash and busybox ash.
        kill = f'for f in {pidfiles}; do [ -f "$f" ] || continue; kill -9 -$(cat "$f") 2>/dev/null; rm -f "$f"; done'
        result = _docker("exec", self.container, "sh", "-c", kill, timeout_s=KILL_TIMEOUT_S)
        if result.returncode != 0:
            raise DockerError(f"cannot kill leftover commands in {self.container}: {result.stderr.strip()}")

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
