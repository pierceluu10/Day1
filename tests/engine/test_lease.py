"""One runner per hire: the lease is exclusive, renewed while held, and expires when its owner dies."""

from __future__ import annotations

import time

import pytest

from dayone.contracts import Machine
from dayone.engine.runner import LEASE_RENEW_S, LEASE_TTL_S, Lease, LeaseHeld, LeaseLost, drive
from dayone.store.atlas import AtlasStore
from tests.engine.fakes import FakeBrowser, FakeGitea, FakeMemory, parts_opener

SHORT_TTL_S = 1.0


def test_second_runner_refuses_while_first_holds(store: AtlasStore) -> None:
    with Lease(store, "ada"):
        with pytest.raises(LeaseHeld):
            with Lease(store, "ada", wait_s=0):
                pass
        with pytest.raises(LeaseHeld):
            drive(
                store, "ada", Machine.UBUNTU, False, parts_opener(store, [], FakeGitea(), FakeMemory(), FakeBrowser())
            )
    # Released on exit: the next runner gets it immediately.
    with Lease(store, "ada", wait_s=0):
        pass


def test_held_lease_is_renewed_past_its_ttl(store: AtlasStore) -> None:
    with Lease(store, "grace") as lease:
        time.sleep(LEASE_TTL_S + 1)
        assert not store.acquire_lease("grace", "intruder", LEASE_TTL_S)
        lease.ensure()


def test_dead_owners_lease_expires(store: AtlasStore) -> None:
    assert store.acquire_lease("linus", "killed-runner", SHORT_TTL_S)
    assert not store.acquire_lease("linus", "resumer", SHORT_TTL_S)
    time.sleep(SHORT_TTL_S + 0.5)
    assert store.acquire_lease("linus", "resumer", SHORT_TTL_S)


def test_lease_lost_stops_the_run(store: AtlasStore) -> None:
    with Lease(store, "ken") as lease:
        store.db["hires"].update_one({"_id": "ken"}, {"$set": {"lease_owner": "thief"}})
        time.sleep(LEASE_RENEW_S + 1)
        with pytest.raises(LeaseLost):
            lease.ensure()
