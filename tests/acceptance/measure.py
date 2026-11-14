"""Print each hire's elapsed time, errors, fixes from memory and LLM calls, all from Atlas.

    uv run python tests/acceptance/measure.py            # the database in .env (MONGODB_DB)
    MONGODB_DB=dayone_acceptance uv run python tests/acceptance/measure.py
"""

from __future__ import annotations

from pymongo import MongoClient

from atlas import hires_by_first_run, ledger, numbers
from dayone.config import settings

SECONDS_PER_MINUTE = 60


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "unfinished"
    minutes, rest = divmod(round(seconds), SECONDS_PER_MINUTE)
    return f"{minutes}m {rest:02d}s"


def main() -> None:
    config = settings()
    client: MongoClient = MongoClient(config.mongodb_uri)
    try:
        db = client[config.mongodb_db]
        print(f"database {db.name}")
        print(f"{'hire':10} {'machine':8} {'elapsed':>10} {'errors':>6} {'memory':>6} {'llm':>4}  run_id")
        for hire in hires_by_first_run(db):
            row = numbers(db, hire)
            record = db.hires.find_one({"_id": hire}) or {}
            print(
                f"{hire:10} {record.get('machine', '?'):8} {duration(row.elapsed_s):>10} {row.errors:>6} "
                f"{row.fixes_from_memory:>6} {row.llm_calls:>4}  {row.run_id}"
            )
            for step_id, doc in ledger(db, hire).items():
                if doc.get("fix_source"):
                    print(f"    {step_id:18} {doc['status']:20} {doc['fix_source']:16} | {doc['evidence']}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
