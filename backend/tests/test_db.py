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
        assert {"worktree", "terminal_session", "iterm_lifecycle", "_migration"} <= tables
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
        # terminal_session has FK -> worktree(repo, name). Inserting a session
        # without a matching worktree should fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO terminal_session
                  (repo, worktree_name, role, terminal_kind, window_id, session_id, spawned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("noexist", "nope", "claude", "iterm2", "w1", "s1", "2026-01-01T00:00:00Z"),
            )
    finally:
        conn.close()


# --- migration 011_terminal_session ----------------------------------------


def test_011_renames_table_and_preserves_rows(db_path: Path, tmp_path: Path) -> None:
    """Apply migrations up to 010 manually, seed an ``iterm_session``
    row matching the pre-PR-#109 schema, then apply 011 and assert the
    table is renamed, columns are renamed, rows are preserved, and
    ``terminal_kind`` backfills to ``'iterm2'``.
    """
    from app.db import (
        _PRAGMAS,
        MIGRATIONS_DIR,
        _apply_one,
        _backup_if_stale,
        _ensure_migration_table,
        get_db_path,
    )

    # Replicate apply_migrations_sync but stop at 010 so we can seed
    # the pre-rename state.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_if_stale(db_path)
    conn = sqlite3.connect(db_path)
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    _ensure_migration_table(conn)

    for migration in sorted(MIGRATIONS_DIR.glob("0[01][0-9]*.sql")):
        if migration.name >= "011":
            break
        _apply_one(conn, migration)

    # Seed a worktree + a pre-rename iterm_session row.
    conn.execute(
        "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("repo1", "wt1", str(tmp_path / "wt1"), "main", "2026-01-01T00:00:00Z", "ready"),
    )
    conn.execute(
        "INSERT INTO iterm_session "
        "(repo, worktree_name, role, iterm_window_id, iterm_session_id, "
        " claude_session_uuid, spawned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "repo1", "wt1", "claude", "WINDOW-OLD", "SESSION-OLD",
            "CLAUDE-UUID", "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()

    # Now apply 011 (and anything after).
    db.apply_migrations_sync(db_path)

    conn = sqlite3.connect(db_path)
    try:
        # Old table is gone.
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "terminal_session" in tables
        assert "iterm_session" not in tables

        # Row preserved with renamed columns + terminal_kind backfilled.
        row = conn.execute(
            "SELECT repo, worktree_name, role, terminal_kind, window_id, "
            "       session_id, claude_session_uuid "
            "FROM terminal_session"
        ).fetchone()
        assert row == (
            "repo1", "wt1", "claude", "iterm2", "WINDOW-OLD", "SESSION-OLD",
            "CLAUDE-UUID",
        )

        # Index also renamed.
        idx_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "terminal_session_id_idx" in idx_names
        assert "iterm_session_id_idx" not in idx_names
    finally:
        conn.close()

    # Sanity: get_db_path / _PRAGMAS imports were used; suppress unused-warn.
    _ = get_db_path


# --- migration 012_terminal_session_fk_repair ------------------------------


def test_012_repairs_dangling_fk_target(db_path: Path, tmp_path: Path) -> None:
    """Repro the broken-005 corruption: terminal_session's FK points at
    ``worktree_old_005``. After 012, the FK points at ``worktree`` and
    INSERTs succeed."""
    from app.db import (
        _PRAGMAS,
        MIGRATIONS_DIR,
        _apply_one,
        _ensure_migration_table,
    )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    _ensure_migration_table(conn)

    # Run everything up to but not including 012. (011 creates
    # terminal_session with the FK target name copied from the old
    # iterm_session schema — we'll inject the broken-005 corruption
    # directly so the test repro doesn't depend on a real broken DB.)
    for migration in sorted(MIGRATIONS_DIR.glob("0[01][0-9]*.sql")):
        if migration.name >= "012":
            break
        _apply_one(conn, migration)

    # Simulate the broken-005 corruption by rebuilding terminal_session
    # with a dangling FK target. SQLite has no API to "edit a FK"; we
    # drop and recreate.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        DROP TABLE terminal_session;
        CREATE TABLE terminal_session (
          repo                 TEXT    NOT NULL,
          worktree_name        TEXT    NOT NULL,
          role                 TEXT    NOT NULL CHECK (role IN ('claude','shell')),
          window_id            TEXT    NOT NULL,
          session_id           TEXT    NOT NULL,
          claude_session_uuid  TEXT,
          spawned_at           TEXT    NOT NULL,
          terminal_kind        TEXT    NOT NULL DEFAULT 'iterm2',
          PRIMARY KEY (repo, worktree_name, role),
          FOREIGN KEY (repo, worktree_name)
            REFERENCES worktree_old_005(repo, name)
            ON DELETE CASCADE
        );
        CREATE INDEX terminal_session_id_idx ON terminal_session(session_id);
        """
    )

    # Seed the parent + a preexisting row to confirm copy preserves it.
    conn.execute(
        "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("r", "wt", str(tmp_path / "wt"), "main", "2026-01-01T00:00:00Z", "ready"),
    )
    conn.execute(
        "INSERT INTO terminal_session "
        "(repo, worktree_name, role, terminal_kind, window_id, session_id, "
        " claude_session_uuid, spawned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "r", "wt", "claude", "iterm2", "W1", "S1",
            "CLAUDE-UUID", "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    # Sanity: before 012, an INSERT explodes with the dangling FK.
    with pytest.raises(sqlite3.OperationalError, match="worktree_old_005"):
        conn.execute(
            "INSERT INTO terminal_session "
            "(repo, worktree_name, role, terminal_kind, window_id, session_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r", "wt", "shell", "iterm2", "W1", "S2", "2026-01-01T00:00:00Z"),
        )
    conn.close()

    # Apply 012 (the actual repair).
    db.apply_migrations_sync(db_path)

    # Now an INSERT succeeds — FK target points at worktree.
    conn = sqlite3.connect(db_path)
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    try:
        conn.execute(
            "INSERT INTO terminal_session "
            "(repo, worktree_name, role, terminal_kind, window_id, session_id, spawned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r", "wt", "shell", "iterm2", "W1", "S2", "2026-01-01T00:00:00Z"),
        )
        conn.commit()

        # Original row still there.
        rows = list(
            conn.execute(
                "SELECT role, terminal_kind, window_id, session_id "
                "FROM terminal_session WHERE repo='r' AND worktree_name='wt' "
                "ORDER BY role"
            )
        )
        assert rows == [
            ("claude", "iterm2", "W1", "S1"),
            ("shell", "iterm2", "W1", "S2"),
        ]

        # Schema's FK now references worktree, not worktree_old_005.
        schema_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='terminal_session'"
        ).fetchone()[0]
        assert "worktree_old_005" not in schema_sql
        assert "REFERENCES worktree(" in schema_sql
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


def test_reconciles_orphaned_setting_up_without_path_to_failed(
    db_path: Path, tmp_path: Path
) -> None:
    """Orphaned 'setting_up' row whose path doesn't exist on disk →
    'failed'. Nothing usable for the user to investigate."""
    db.apply_migrations_sync(db_path)

    bogus_path = tmp_path / "does-not-exist"
    assert not bogus_path.exists()

    conn = db.open_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO worktree
              (repo, name, path, branch, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "repo1",
                "wt1",
                str(bogus_path),
                "main",
                "2026-01-01T00:00:00Z",
                "setting_up",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run apply_migrations — reconciliation routes by path-on-disk
    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM worktree WHERE name = 'wt1'"
        ).fetchone()[0]
        assert status == "failed"
    finally:
        conn.close()


def test_reconciles_orphaned_setting_up_with_path_to_code_on_disk(
    db_path: Path, tmp_path: Path
) -> None:
    """Orphaned 'setting_up' row whose path DOES exist on disk →
    'code_on_disk'. `git worktree add` got through before the kill;
    code is usable."""
    db.apply_migrations_sync(db_path)

    real_path = tmp_path / "wt-on-disk"
    real_path.mkdir()

    conn = db.open_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO worktree
              (repo, name, path, branch, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "repo1",
                "wt2",
                str(real_path),
                "main",
                "2026-01-01T00:00:00Z",
                "setting_up",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM worktree WHERE name = 'wt2'"
        ).fetchone()[0]
        assert status == "code_on_disk"
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
                ("r", "cod1", "/p4", "main", "2026-01-01T00:00:00Z", "code_on_disk"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        rows = dict(conn.execute("SELECT name, status FROM worktree"))
        assert rows == {
            "ready1": "ready",
            "failed1": "failed",
            "stale1": "stale",
            "cod1": "code_on_disk",
        }
    finally:
        conn.close()


def test_migration_005_allows_code_on_disk_status(db_path: Path) -> None:
    """The rebuilt CHECK constraint must accept 'code_on_disk'; the
    failed INSERT path is the regression test for the constraint
    actually being applied."""
    db.apply_migrations_sync(db_path)
    conn = db.open_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO worktree
              (repo, name, path, branch, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("r", "cod", "/p", "main", "2026-01-01T00:00:00Z", "code_on_disk"),
        )
        conn.commit()
        # And the row is queryable back as the same value.
        row = conn.execute(
            "SELECT status FROM worktree WHERE name = 'cod'"
        ).fetchone()
        assert row[0] == "code_on_disk"

        # Negative: an unknown status is still rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO worktree
                  (repo, name, path, branch, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("r", "bad", "/p", "main", "2026-01-01T00:00:00Z", "nope"),
            )
    finally:
        conn.close()


def test_migration_006_pr_state_fk_points_at_worktree(db_path: Path) -> None:
    """After all migrations apply, pr_state's FK target is `worktree`
    (not a dangling `worktree_old_005`). Insert smoke-test confirms
    the FK resolves at INSERT time."""
    db.apply_migrations_sync(db_path)
    conn = db.open_db(db_path)
    try:
        # PRAGMA foreign_key_list returns rows describing each FK; the
        # third column (index 2) is the target table name.
        fks = conn.execute("PRAGMA foreign_key_list(pr_state)").fetchall()
        assert fks, "pr_state should have FK to worktree"
        for fk in fks:
            assert fk[2] == "worktree", (
                f"pr_state FK target must be 'worktree', got {fk[2]!r}"
            )

        # End-to-end: seed a worktree, then insert a pr_state row that
        # references it. The INSERT would error with "no such table"
        # if the FK target were dangling.
        conn.execute(
            """
            INSERT INTO worktree
              (repo, name, path, branch, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("r", "wt", "/p", "main", "2026-01-01T00:00:00Z", "ready"),
        )
        conn.execute(
            """
            INSERT INTO pr_state
              (repo, worktree_name, headline, payload, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("r", "wt", "no_pr", "{}", "2026-01-01T00:00:00Z"),
        )
        conn.commit()

        # And cascade-delete still works (the FK action survived the
        # rebuild).
        conn.execute("DELETE FROM worktree WHERE name = 'wt'")
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM pr_state WHERE worktree_name = 'wt'"
        ).fetchone()[0]
        assert remaining == 0
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
