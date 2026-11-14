"""Database access. `python -m acme_api.db init` creates the schema."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2

from acme_api.settings import ROOT, settings

SQLITE_PREFIX = "sqlite:///"
POSTGRES_PREFIX = "postgresql://"

SCHEMAS = {
    "sqlite": "CREATE TABLE IF NOT EXISTS users ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL)",
    "postgresql": "CREATE TABLE IF NOT EXISTS users ("
    "id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL)",
}
PLACEHOLDERS = {"sqlite": "?", "postgresql": "%s"}
INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)


class UnsupportedDatabase(ValueError):
    pass


def dialect(url: str) -> str:
    if url.startswith(SQLITE_PREFIX):
        return "sqlite"
    if url.startswith(POSTGRES_PREFIX):
        return "postgresql"
    raise UnsupportedDatabase(f"unsupported database url {url!r}; use {SQLITE_PREFIX}... or {POSTGRES_PREFIX}...")


def _open(url: str) -> Any:
    if dialect(url) == "sqlite":
        return sqlite3.connect(ROOT / url.removeprefix(SQLITE_PREFIX))
    return psycopg2.connect(url)


@contextmanager
def connection() -> Iterator[Any]:
    conn = _open(settings().database_url)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def execute(conn: Any, sql: str, params: tuple = ()) -> Any:
    """Run `sql` written with `?` placeholders on either dialect."""
    cursor = conn.cursor()
    cursor.execute(sql.replace("?", PLACEHOLDERS[dialect(settings().database_url)]), params)
    return cursor


def init() -> None:
    with connection() as conn:
        execute(conn, SCHEMAS[dialect(settings().database_url)])


def main(argv: list[str]) -> int:
    if argv != ["init"]:
        print("usage: python -m acme_api.db init", file=sys.stderr)
        return 2
    init()
    print(f"created schema for {settings().database_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
