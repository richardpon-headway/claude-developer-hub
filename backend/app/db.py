"""SQLite connection helpers, migration runner, and startup reconciliation.

CDH ships a hand-rolled migration runner rather than depending on Alembic;
the cost of Alembic isn't justified for a single-user local app with a
handful of forward-only migrations.

Behavior on startup (driven by ``apply_migrations`` from the FastAPI
lifespan hook):

1. Ensure the parent directory exists.
2. If ``cdh.db`` exists and the most recent backup is older than 24h
   (or no backup exists), copy the file to a sibling
   ``cdh.db.bak.<ISO timestamp>``. Cap retained backups at 7.
3. Open the DB with the project PRAGMAs (WAL, NORMAL, FK on, busy 5s).
4. Ensure the ``_migration`` tracking table exists.
5. Apply every ``NNN_*.sql`` file under ``migrations/`` that isn't
   already recorded, in lexical order. Each file is responsible for its
   own BEGIN/COMMIT.
6. Reconcile orphaned ``worktree`` rows whose ``status`` is still
   ``setting_up`` (a process kill mid-setup) to ``failed`` so the UI
   can offer retry affordances.

The whole startup path is sync (sqlite3 is sync-only); we wrap it in
``asyncio.to_thread`` so the event loop isn't blocked.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path.home() / "Library" / "Application Support" / "cdh" / "cdh.db"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_db_path() -> Path:
    """Return the DB path to use right now.

    Looks up CDH_DB_PATH at call time so tests can override via
    ``monkeypatch.setenv``. Production reads ``DB_PATH``.
    """
    env_override = os.environ.get("CDH_DB_PATH")
    if env_override:
        return Path(env_override).expanduser()
    return DB_PATH

# Backups kept at 7. New backup created only when none exists or the most
# recent is older than this threshold.
MAX_BACKUPS = 7
BACKUP_STALE_AFTER_SECONDS = 24 * 60 * 60

_MIGRATION_FILE_RE = re.compile(r"^\d{3,}_[A-Za-z0-9_]+\.sql$")

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


def open_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the project PRAGMAs applied.

    Caller is responsible for closing the connection. PRAGMAs are
    session-scoped (foreign_keys, busy_timeout); WAL/synchronous persist
    in the database file once set.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migration (
          id         INTEGER PRIMARY KEY,
          name       TEXT NOT NULL UNIQUE,
          applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM _migration")
    return {row[0] for row in cur.fetchall()}


def _discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    files = [p for p in directory.glob("*.sql") if _MIGRATION_FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: p.name)


def _backup_if_stale(db_path: Path) -> Path | None:
    """Create a timestamped backup if the DB file exists and no recent backup
    is present. Returns the backup path (or None if not made). Prunes to
    MAX_BACKUPS oldest first.
    """
    if not db_path.exists():
        return None

    parent = db_path.parent
    backup_prefix = f"{db_path.name}.bak."
    existing = sorted(parent.glob(f"{backup_prefix}*"), key=lambda p: p.stat().st_mtime)
    now = datetime.now(timezone.utc).timestamp()

    if existing:
        newest_mtime = existing[-1].stat().st_mtime
        if (now - newest_mtime) < BACKUP_STALE_AFTER_SECONDS:
            return None

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    backup = parent / f"{backup_prefix}{stamp}"
    shutil.copy2(db_path, backup)
    log.info("backed up %s -> %s", db_path, backup)

    existing = sorted(parent.glob(f"{backup_prefix}*"), key=lambda p: p.stat().st_mtime)
    excess = existing[:-MAX_BACKUPS] if len(existing) > MAX_BACKUPS else []
    for p in excess:
        p.unlink()
    return backup


def _apply_one(conn: sqlite3.Connection, path: Path) -> None:
    """Apply a single migration script and record it in _migration.

    The script is responsible for its own BEGIN/COMMIT — Python sqlite3's
    executescript implicitly COMMITs before running, which is why we can't
    wrap from outside. On any failure the script's transaction is rolled
    back and we re-raise. The _migration INSERT runs after executescript;
    a failure between the two would leave the schema applied but unrecorded
    (a known small risk acceptable for a single-user local DB).
    """
    sql = path.read_text(encoding="utf-8")
    try:
        conn.executescript(sql)
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    conn.execute(
        "INSERT INTO _migration (name, applied_at) VALUES (?, ?)",
        (path.name, _now_iso()),
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _reconcile_orphaned_setting_up(conn: sqlite3.Connection) -> int:
    """Mark any worktree rows in 'setting_up' as 'failed'.

    Such rows are leftovers from a process kill while a worktree was
    being created. The workspace page offers retry affordances on
    'failed' rows. Returns the number of rows updated.
    """
    cur = conn.execute(
        "UPDATE worktree SET status = 'failed' WHERE status = 'setting_up'"
    )
    conn.commit()
    if cur.rowcount:
        log.info("reconciled %d orphaned 'setting_up' worktree(s) -> 'failed'", cur.rowcount)
    return cur.rowcount


def apply_migrations_sync(db_path: Path | None = None) -> None:
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_if_stale(db_path)

    conn = open_db(db_path)
    try:
        _ensure_migration_table(conn)
        applied = _applied_migrations(conn)
        pending = [p for p in _discover_migrations() if p.name not in applied]
        for migration in pending:
            log.info("applying migration %s", migration.name)
            _apply_one(conn, migration)

        if _table_exists(conn, "worktree"):
            _reconcile_orphaned_setting_up(conn)
    finally:
        conn.close()


async def apply_migrations(db_path: Path | None = None) -> None:
    """Async-friendly wrapper around ``apply_migrations_sync``.

    The lifespan hook calls this. sqlite3 is sync, so we offload to a
    thread to keep the event loop responsive while the DB opens/migrates.
    """
    if db_path is None:
        db_path = get_db_path()
    await asyncio.to_thread(apply_migrations_sync, db_path)
