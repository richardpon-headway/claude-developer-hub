"""Tests for the SQLite migration runner and reconciliation."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app import db


@pytest.fixture(autouse=True)
def _isolate() -> None:
    """Override the top-level autouse ``_isolate`` fixture from
    ``conftest.py`` — those tests want a fresh-migrated DB before each
    test, but this file's tests are testing the migration runner
    itself and need to control DB creation manually."""
    return None


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cdh-test.db"


# --- fresh apply / idempotency ---------------------------------------------


def test_apply_creates_db_and_records_migrations(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM _migration")}
        assert "001_initial.sql" in names

        # Tables from 001_initial.sql exist
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"worktree", "iterm_session", "iterm_lifecycle", "_migration"} <= tables
    finally:
        conn.close()


def test_apply_is_idempotent(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    db.apply_migrations_sync(db_path)  # second call: no-op

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM _migration WHERE name = '001_initial.sql'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_async_wrapper_runs(db_path: Path) -> None:
    asyncio.run(db.apply_migrations(db_path))
    assert db_path.exists()


# --- PRAGMAs ---------------------------------------------------------------


def test_open_db_applies_pragmas(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    conn = db.open_db(db_path)
    try:
        # WAL persists in the file header
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"

        # FKs and busy_timeout are session-scoped — set on every open
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_foreign_keys_actually_enforced(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    conn = db.open_db(db_path)
    try:
        # iterm_session has FK -> worktree(repo, name). Inserting a session
        # without a matching worktree should fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO iterm_session
                  (repo, worktree_name, role, iterm_window_id, iterm_session_id, spawned_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("noexist", "nope", "claude", "w1", "s1", "2026-01-01T00:00:00Z"),
            )
    finally:
        conn.close()


# --- backups ---------------------------------------------------------------


def test_no_backup_on_first_apply(db_path: Path, tmp_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    backups = list(tmp_path.glob(f"{db_path.name}.bak.*"))
    assert backups == []  # nothing to back up before first run


def test_backup_created_on_second_run(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)  # creates DB; nothing to back up

    # Second run: DB exists, no prior backups → backup created
    db.apply_migrations_sync(db_path)

    backups = list(db_path.parent.glob(f"{db_path.name}.bak.*"))
    assert len(backups) == 1
    assert backups[0].exists()


def test_backup_skipped_when_recent(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    first = db._backup_if_stale(db_path)  # DB exists, no backup → creates one
    assert first is not None
    second = db._backup_if_stale(db_path)  # newest backup is seconds old
    assert second is None


def test_backup_retention_caps_at_seven(db_path: Path, tmp_path: Path) -> None:
    db.apply_migrations_sync(db_path)

    # Create 10 fake-old backups manually, oldest first
    parent = db_path.parent
    for i in range(10):
        fake = parent / f"{db_path.name}.bak.fake-{i:02d}"
        fake.write_bytes(b"x")
        # Age each one so the next _backup_if_stale considers all stale
        os.utime(fake, (time.time() - 86400 * (10 - i), time.time() - 86400 * (10 - i)))

    # Trigger another backup; should prune to 7 (6 existing + 1 new = 7)
    db._backup_if_stale(db_path)
    backups = sorted(parent.glob(f"{db_path.name}.bak.*"))
    assert len(backups) == db.MAX_BACKUPS


# --- reconciliation --------------------------------------------------------


def test_reconciles_orphaned_setting_up(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)

    # Pre-seed a 'setting_up' row to simulate a process kill mid-setup
    conn = db.open_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO worktree
              (repo, name, path, branch, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("repo1", "wt1", "/tmp/wt1", "main", "2026-01-01T00:00:00Z", "setting_up"),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run apply_migrations — reconciliation should flip 'setting_up' -> 'failed'
    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM worktree WHERE name = 'wt1'"
        ).fetchone()[0]
        assert status == "failed"
    finally:
        conn.close()


def test_reconciliation_leaves_other_statuses_alone(db_path: Path) -> None:
    db.apply_migrations_sync(db_path)
    conn = db.open_db(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO worktree
              (repo, name, path, branch, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("r", "ready1", "/p1", "main", "2026-01-01T00:00:00Z", "ready"),
                ("r", "failed1", "/p2", "main", "2026-01-01T00:00:00Z", "failed"),
                ("r", "stale1", "/p3", "main", "2026-01-01T00:00:00Z", "stale"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        rows = dict(conn.execute("SELECT name, status FROM worktree"))
        assert rows == {"ready1": "ready", "failed1": "failed", "stale1": "stale"}
    finally:
        conn.close()


# --- migration discovery ---------------------------------------------------


def test_migration_filename_regex_filters_correctly(tmp_path: Path) -> None:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "001_initial.sql").write_text("BEGIN; COMMIT;")
    (d / "002_add_foo.sql").write_text("BEGIN; COMMIT;")
    (d / "junk.sql").write_text("BEGIN; COMMIT;")  # no numeric prefix → ignored
    (d / "003_bad-dashes.sql").write_text("BEGIN; COMMIT;")  # dashes → ignored
    (d / "001_initial.sql.bak").write_text("nope")  # not .sql ext → ignored

    found = [p.name for p in db._discover_migrations(d)]
    assert found == ["001_initial.sql", "002_add_foo.sql"]
