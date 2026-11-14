"""Reset returns a hire to day zero everywhere, so its next run's checks fail before they pass."""

from __future__ import annotations

import pytest

from dayone.contracts import CheckResult, EventType, Hire, Machine, StepStatus, container_name
from dayone.engine.runner import Lease, LeaseHeld, ResetParts, reset_hires
from dayone.store.atlas import AtlasStore
from dayone.store.events import AtlasEventBus


class Recorder:
    def __init__(self) -> None:
        self.gitea: list[str] = []
        self.containers: list[str] = []
        self.profiles: list[str] = []

    def parts(self) -> ResetParts:
        return ResetParts(
            reset_gitea=self.gitea.append,
            remove_container=lambda name: self.containers.append(container_name(name)),
            remove_browser_profile=self.profiles.append,
        )


def _seed(store: AtlasStore, name: str) -> None:
    hire = Hire(name=name, machine=Machine.UBUNTU, container=container_name(name), run_id="r1")
    store.upsert_hire(hire)
    store.record_check(name, "t_base_tools", CheckResult(ok=True, evidence="git 2.34"))
    store.mark_done(name, "t_base_tools", StepStatus.VERIFIED, CheckResult(ok=True, evidence="git 2.34"), None)
    events = AtlasEventBus(store.db, hire)
    events.emit(EventType.RUN_STARTED)
    events.close()
    store.db["experiences"].insert_one({"hire": name, "fingerprint": "fp"})


def _left(store: AtlasStore, name: str) -> tuple[int, int, int]:
    return (
        store.db["hires"].count_documents({"_id": name}),
        store.db["steps"].count_documents({"hire": name}),
        store.db["events"].count_documents({"hire": name}),
    )


def test_reset_one_hire_clears_gitea_container_and_ledger(store: AtlasStore) -> None:
    _seed(store, "ada")
    _seed(store, "grace")
    recorder = Recorder()
    assert reset_hires(store, "ada", recorder.parts()) == ["ada"]
    assert recorder.gitea == ["ada"] and recorder.containers == ["dayone-ada"] and recorder.profiles == ["ada"]
    assert _left(store, "ada") == (0, 0, 0)
    assert _left(store, "grace") == (1, 1, 1), "other hires untouched"
    assert store.db["experiences"].count_documents({}) == 2, "experiences are memory and survive reset"


def test_reset_all_covers_every_hire_in_atlas(store: AtlasStore) -> None:
    _seed(store, "ada")
    _seed(store, "ken")
    recorder = Recorder()
    assert reset_hires(store, None, recorder.parts()) == ["ada", "ken"]
    assert recorder.gitea == ["ada", "ken"] and recorder.containers == ["dayone-ada", "dayone-ken"]
    assert _left(store, "ada") == _left(store, "ken") == (0, 0, 0)


def test_hire_without_atlas_doc_is_still_fully_reset(store: AtlasStore) -> None:
    # e.g. a container another session created from its own dev database
    recorder = Recorder()
    reset_hires(store, "linus", recorder.parts())
    assert recorder.gitea == ["linus"] and recorder.profiles == ["linus"] and recorder.containers == ["dayone-linus"]
    assert _left(store, "linus") == (0, 0, 0)


def test_reset_refuses_while_any_hire_is_running_and_touches_nothing(store: AtlasStore) -> None:
    _seed(store, "ada")
    _seed(store, "barbara")
    recorder = Recorder()
    with Lease(store, "barbara"):
        with pytest.raises(LeaseHeld):
            reset_hires(store, None, recorder.parts())
    assert recorder.gitea == [] and recorder.containers == [] and recorder.profiles == []
    assert _left(store, "ada") == (1, 1, 1)
    assert store.acquire_lease("ada", "next-runner", 1.0), "reset released the leases it took"
