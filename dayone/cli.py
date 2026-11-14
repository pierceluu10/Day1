"""`dayone start | resume | reset | serve`."""

from __future__ import annotations

import argparse
import sys

from dayone.api import processes, queries
from dayone.config import MissingSetting, settings
from dayone.contracts import HIRES, Machine

EXIT_OK = 0
EXIT_RUN_FAILED = 1  # argparse exits 2 on bad arguments
EXIT_NOTHING_TO_RESUME = 3
EXIT_ALREADY_RUNNING = 4
EXIT_MISSING_SETTING = 5

API_HOST = "127.0.0.1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dayone", description="Onboard a new hire end to end.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start a new run for a hire")
    start.add_argument("--hire", required=True, choices=HIRES)
    start.add_argument("--machine", required=True, choices=[m.value for m in Machine])
    resume = commands.add_parser("resume", help="resume a hire's run (default: the most recent unfinished one)")
    resume.add_argument("--hire", choices=HIRES)
    reset = commands.add_parser("reset", help="forget a hire's run (default: every hire)")
    reset.add_argument("--hire", choices=HIRES)
    commands.add_parser("serve", help="serve the page and API")
    return parser


def _unfinished_hire() -> str | None:
    client, db = queries.connect(settings())
    try:
        return queries.latest_unfinished_hire(db)
    finally:
        client.close()


def _run(hire: str, machine: Machine | None, resume: bool) -> int:
    if (pid := processes.running_pid(hire)) is not None:
        print(f"dayone: {hire} is already running (pid {pid})", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    from dayone.engine.runner import run_hire  # engine imports Playwright and Docker wiring; `serve` never needs it

    processes.record_pid(hire)
    try:
        return EXIT_OK if run_hire(hire, machine, resume) else EXIT_RUN_FAILED
    finally:
        processes.clear_pid(hire)


def _reset(hire: str | None) -> int:
    busy = [(name, pid) for name in ([hire] if hire else HIRES) if (pid := processes.running_pid(name)) is not None]
    if busy:
        print(f"dayone: stop the running engine first: {busy}", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    from dayone.engine.runner import reset

    reset(hire)
    return EXIT_OK


def _serve() -> int:
    import uvicorn

    uvicorn.run("dayone.api.app:app", host=API_HOST, port=settings().api_port)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        match args.command:
            case "start":
                return _run(args.hire, Machine(args.machine), resume=False)
            case "resume":
                hire = args.hire or _unfinished_hire()
                if hire is None:
                    print("dayone: no unfinished hire to resume", file=sys.stderr)
                    return EXIT_NOTHING_TO_RESUME
                return _run(hire, None, resume=True)
            case "reset":
                return _reset(args.hire)
            case "serve":
                return _serve()
    except MissingSetting as error:
        print(f"dayone: {error}", file=sys.stderr)
        return EXIT_MISSING_SETTING
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
