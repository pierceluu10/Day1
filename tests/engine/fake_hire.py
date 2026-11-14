"""Runs the real engine (drive + Atlas store) on the fake two-step task in its own process, so tests can kill -9 it.

Usage: python -m tests.engine.fake_hire HIRE WORKDIR start|resume   (MONGODB_DB selects the throwaway database)
"""

from __future__ import annotations

import sys
from pathlib import Path

from dayone.config import settings
from dayone.contracts import Machine
from dayone.engine.runner import drive
from dayone.store.atlas import AtlasStore
from tests.engine.fakes import FakeBrowser, FakeGitea, FakeMemory, marker_step, parts_opener

STEP_IDS = ("s_one", "s_two")


def main() -> int:
    hire, workdir, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    store = AtlasStore(settings())
    try:
        steps = [marker_step(workdir, step_id) for step_id in STEP_IDS]
        opener = parts_opener(store, steps, FakeGitea(), FakeMemory(), FakeBrowser())
        resume = mode == "resume"
        ok = drive(store, hire, None if resume else Machine.UBUNTU, resume, opener)
    finally:
        store.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
