"""The numbers the page shows, computed from real Atlas documents."""

from __future__ import annotations

from dayone.api import queries
from dayone.contracts import STEP_IDS


def test_run_summary_counts_this_run_and_the_ledger(db, event):
    db.hires.insert_one({"name": "ada", "machine": "ubuntu"})
    db.events.insert_many(
        [
            event("ada", "old", "run_started", 0),
            event("ada", "old", "error_captured", 1, "t_runtime"),
            event("ada", "new", "run_started", 100),
            event("ada", "new", "error_captured", 110, "t_runtime"),
            event("ada", "new", "error_captured", 120, "t_init_db"),
            event("ada", "new", "run_resumed", 140),
            event("ada", "new", "run_finished", 652),
        ]
    )
    db.steps.insert_many(
        [
            {"hire": "ada", "step_id": "t_runtime", "status": "fixed_from_memory", "fix_source": "exact_memory", "llm_calls": 0},
            {"hire": "ada", "step_id": "t_system_package", "status": "fixed_from_memory", "fix_source": "semantic_memory", "llm_calls": 0},
            {"hire": "ada", "step_id": "t_init_db", "status": "fixed_by_reasoning", "fix_source": "reasoning", "llm_calls": 2},
            {"hire": "ada", "step_id": "t_clone", "status": "verified", "fix_source": None},  # older docs lack llm_calls
            {"hire": "ken", "step_id": "t_runtime", "status": "fixed_by_reasoning", "fix_source": "reasoning", "llm_calls": 5},
        ]
    )

    summary = queries.run_summary(db, "ada")
    print(summary)

    assert summary["run_id"] == "new"
    assert summary["machine"] == "ubuntu"
    assert summary["elapsed_s"] == 552.0  # includes the time the run was killed
    assert summary["errors"] == 2
    assert summary["fixes_from_memory"] == 2
    assert summary["llm_calls"] == 2
    assert summary["ok"] is True


def test_failed_run_resumed_to_success_is_ok(db, event):
    db.events.insert_many(
        [
            event("ken", "k", "run_started", 0),
            event("ken", "k", "run_failed", 30),
            event("ken", "k", "run_resumed", 60),
            event("ken", "k", "run_finished", 90),
        ]
    )

    summary = queries.run_summary(db, "ken")

    assert summary["ok"] is True
    assert summary["elapsed_s"] == 90.0


def test_failed_run_being_resumed_is_unfinished(db, event):
    db.events.insert_many(
        [event("ken", "k", "run_started", 0), event("ken", "k", "run_failed", 30), event("ken", "k", "run_resumed", 60)]
    )

    assert queries.run_summary(db, "ken")["finished_at"] is None
    assert queries.latest_unfinished_hire(db) == "ken"


def test_unfinished_run_has_no_outcome_and_counts_elapsed_to_now(db, event):
    db.events.insert_one(event("ken", "r1", "run_started", 0))

    summary = queries.run_summary(db, "ken")

    assert summary["finished_at"] is None
    assert summary["ok"] is None
    assert summary["elapsed_s"] > 0


def test_failed_run_is_finished_but_not_ok(db, event):
    db.events.insert_many([event("ken", "r1", "run_started", 0), event("ken", "r1", "run_failed", 30)])

    summary = queries.run_summary(db, "ken")

    assert summary["ok"] is False
    assert summary["elapsed_s"] == 30.0


def test_events_after_pages_through_same_millisecond_events_without_loss(db, event):
    db.events.insert_many([event("barbara", "r1", "terminal_output", 5, "t_clone", chunk=str(i)) for i in range(5)])

    first = queries.events_after(db, "barbara", None, limit=2)
    rest = queries.events_after(db, "barbara", first[-1]["_id"], limit=10)

    chunks = [doc["data"]["chunk"] for doc in first + rest]
    assert chunks == ["0", "1", "2", "3", "4"]


def test_events_after_is_scoped_to_the_hire(db, event):
    db.events.insert_many([event("ada", "a", "run_started", 0), event("ken", "k", "run_started", 0)])

    assert [doc["hire"] for doc in queries.events_after(db, "ken", None, limit=10)] == ["ken"]


def test_steps_are_in_run_order_and_unseen_steps_are_not_started(db):
    db.steps.insert_one({"hire": "ada", "step_id": "t_clone", "status": "verified", "run_count": 1, "evidence": "HEAD ok"})

    steps = queries.steps(db, "ada")

    assert [s["step_id"] for s in steps] == list(STEP_IDS)
    by_id = {s["step_id"]: s for s in steps}
    assert by_id["t_clone"]["status"] == "verified"
    assert by_id["t_base_tools"]["status"] == "not_started"


def test_compare_uses_the_first_successful_run_as_hire_one(db, event):
    db.events.insert_many(
        [
            event("grace", "g", "run_started", 0),
            event("grace", "g", "run_failed", 10),  # failed earlier: not hire 1
            event("ada", "a", "run_started", 5),
            event("ada", "a", "run_finished", 50),
            event("linus", "l", "run_started", 20),
            event("linus", "l", "run_finished", 40),  # finished first: hire 1
            event("barbara", "b", "run_started", 60),
        ]
    )

    comparison = queries.compare(db, "barbara")

    assert comparison["first"]["hire"] == "linus"
    assert comparison["current"]["hire"] == "barbara"


def test_compare_without_any_finished_run(db, event):
    db.events.insert_one(event("ada", "a", "run_started", 0))

    assert queries.compare(db, "ada")["first"] is None


def test_latest_unfinished_hire_picks_the_most_recently_started(db, event):
    db.events.insert_many(
        [
            event("ada", "a", "run_started", 0),
            event("ada", "a", "run_finished", 10),
            event("barbara", "b", "run_started", 20),
            event("ken", "k", "run_started", 30),
        ]
    )

    assert queries.latest_unfinished_hire(db) == "ken"
    db.events.insert_one(event("ken", "k", "run_finished", 40))
    assert queries.latest_unfinished_hire(db) == "barbara"


def test_no_hire_to_resume_when_everything_finished(db, event):
    db.events.insert_many([event("ada", "a", "run_started", 0), event("ada", "a", "run_finished", 10)])

    assert queries.latest_unfinished_hire(db) is None
