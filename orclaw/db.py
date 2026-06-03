"""SQLite layer.

Thin wrapper around :mod:`sqlite3` that opens the engine database with
sensible pragmas, exposes a :func:`connect` context manager, and applies the
schema on first use.

The schema lives in ``orchestrator/state/schema.sql`` (legacy location from
sprint 0 docs) and is copied here at import time for `init_db` to use.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from orclaw.exceptions import DatabaseError
from orclaw.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger(__name__)


SCHEMA_PATH = Path(__file__).parent.parent / "orchestrator" / "state" / "schema.sql"


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """Return rows as dicts keyed by column name (more ergonomic than tuples)."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


@contextmanager
def connect(db_path: Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with our standard pragmas.

    Use as::

        with connect(settings.paths.db_path) as conn:
            cur = conn.execute("SELECT * FROM batches")
            ...

    Pragmas applied:

    - ``journal_mode = WAL`` for concurrent readers
    - ``foreign_keys = ON``
    - ``synchronous = NORMAL`` (faster, still durable enough for our use)
    - row_factory returns dicts
    """
    if not db_path.parent.exists():
        raise DatabaseError(
            f"Data directory {db_path.parent} does not exist. Run `orclaw db init` first."
        )

    uri = f"file:{db_path}?mode=ro" if read_only else f"file:{db_path}?mode=rwc"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = _row_factory  # type: ignore[assignment]

    try:
        # Pragmas only stick on read-write connections.
        if not read_only:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path, *, schema_path: Path = SCHEMA_PATH) -> None:
    """Create the database file and apply the schema + migrations.

    Idempotent: running twice on an existing DB is a no-op because every
    DDL statement in :data:`SCHEMA_PATH` uses ``IF NOT EXISTS`` and
    :func:`apply_migrations` only ever adds what's missing.
    """
    if not schema_path.is_file():
        raise DatabaseError(f"Schema file not found at {schema_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = schema_path.read_text()
    with connect(db_path) as conn:
        conn.executescript(schema_sql)
        apply_migrations(conn)
        log.info("db_initialised", path=str(db_path), schema=str(schema_path))


# --- Migrations ------------------------------------------------------------

#: Tables that carry the multi-repo ``repo`` dimension (Phase 1).
_REPO_TABLES = ("batches", "runs", "reviews")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table`` (empty set if the table doesn't exist)."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema, additively.

    ``CREATE TABLE IF NOT EXISTS`` in the schema file never alters an
    existing table, so column additions live here. Safe to run on every
    init: each step checks before it acts.

    Phase 1 (multi-repo foundation):
      - add a ``repo`` column to batches/runs/reviews (default ''),
      - swap the batches active-uniqueness index from ``(issue_number)``
        to ``(repo, issue_number)`` so two repos can share issue numbers,
      - add the ``(repo, status)`` / ``(repo)`` helper indexes.
    """
    for table in _REPO_TABLES:
        cols = _columns(conn, table)
        if cols and "repo" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN repo TEXT NOT NULL DEFAULT ''")
            log.info("db_migration_applied", change="add_repo_column", table=table)

    # Replace the legacy single-repo unique index with the composite one.
    conn.execute("DROP INDEX IF EXISTS idx_batches_issue_active")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_repo_issue_active "
        "ON batches(repo, issue_number) WHERE status != 'failed' AND status != 'skipped'"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batches_repo_status ON batches(repo, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo)")


def backfill_repo(conn: sqlite3.Connection, repo: str) -> int:
    """Stamp legacy rows (``repo = ''``) with the given target repo.

    Returns the number of rows updated across all repo-bearing tables.
    Run once after migrating a single-repo install so its history joins
    the multi-repo world under the right slug. No-op for ``repo=''``.
    """
    if not repo:
        return 0
    updated = 0
    for table in _REPO_TABLES:
        if "repo" not in _columns(conn, table):
            continue
        cur = conn.execute(f"UPDATE {table} SET repo = ? WHERE repo = ''", (repo,))
        updated += cur.rowcount
    if updated:
        log.info("db_backfill_repo", repo=repo, rows=updated)
    return updated


# --- Tiny query helpers used by sprint-2+ code -----------------------------


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    """Run a SELECT and return the first row (or None)."""
    cur = conn.execute(sql, params)
    return cur.fetchone()


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return all rows."""
    cur = conn.execute(sql, params)
    return cur.fetchall()
