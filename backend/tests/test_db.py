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


def test_migration_006_pr_state_fk_points_at_worktree(
    db_path: Path, tmp_path: Path
) -> None:
    """At migration 012's schema, pr_state's FK target is `worktree`
    (not a dangling `worktree_old_005`). Migration 013 supersedes this
    by rekeying pr_state to GitHub identity — to keep this regression
    pin meaningful for the 005/006 incident, we stop applying at 012.
    """
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
    for migration in sorted(MIGRATIONS_DIR.glob("0[01][0-9]*.sql")):
        if migration.name >= "013":
            break
        _apply_one(conn, migration)

    try:
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

        # Cascade-delete still works.
        conn.execute("DELETE FROM worktree WHERE name = 'wt'")
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM pr_state WHERE worktree_name = 'wt'"
        ).fetchone()[0]
        assert remaining == 0
    finally:
        conn.close()


# --- migration 013_unified_pr ----------------------------------------------


def _apply_through_012(
    db_path: Path,
) -> sqlite3.Connection:
    """Apply migrations 001 through 012 to a fresh DB, return an open
    connection so the test can seed legacy table state before invoking
    013. The connection has PRAGMAs applied + the _migration tracking
    table created — equivalent to apply_migrations_sync's setup path.
    """
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
    for migration in sorted(MIGRATIONS_DIR.glob("0[01][0-9]*.sql")):
        if migration.name >= "013":
            break
        _apply_one(conn, migration)
    return conn


def test_migrations_fold_legacy_tables_then_drop_inbox(
    db_path: Path, tmp_path: Path
) -> None:
    """Seed rows across all four legacy PR-keyed tables plus a worktree-
    only PR, then run the full migration chain (013 folds them into
    ``pr``; 014 removes the inbox and drops the inbox-only rows). Six
    seeded cases:

    1. bookmark-only with notes              → survives
    2. inbox-only                            → dropped by 014
    3. inbox_archived shadowing an inbox row → dropped by 014
    4. bookmark + (former inbox) overlap     → survives (bookmarked)
    5. worktree + bookmark overlap           → survives
    6. authored_pr_notes-only                → survives (has notes)

    Also asserts the rebuilt schema: pr_author_login column gone from
    worktree, four legacy tables absent from sqlite_master, the
    inbox/archive columns gone from pr, and pr_state rekeyed.
    """
    conn = _apply_through_012(db_path)
    try:
        # (1) bookmark-only with notes
        conn.execute(
            "INSERT INTO bookmark (pr_repo, pr_number, title, author_login, "
            "url, state, notes, ticket, bookmarked_at, last_refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "o/r", 1, "B-only", "alice", "https://gh/o/r/pull/1",
                "open", "bookmark-notes", "TICK-1",
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
            ),
        )
        # (2) inbox-only
        conn.execute(
            "INSERT INTO inbox (pr_repo, pr_number, title, author_login, "
            "url, is_draft, ci_status, sources, notes, ticket, "
            "pr_updated_at, added_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "o/r", 2, "I-only", "bob", "https://gh/o/r/pull/2",
                0, "pass", '["reviewer"]', None, None,
                "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z",
                "2026-01-03T00:00:00Z",
            ),
        )
        # (3) inbox_archived shadowing an inbox row
        conn.execute(
            "INSERT INTO inbox (pr_repo, pr_number, title, author_login, "
            "url, is_draft, ci_status, sources, notes, ticket, "
            "pr_updated_at, added_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "o/r", 3, "I-arch", "bob", "https://gh/o/r/pull/3",
                0, "pass", '["assignee"]', None, None,
                "2026-01-04T00:00:00Z", "2026-01-04T00:00:00Z",
                "2026-01-04T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO inbox_archived (pr_repo, pr_number, archived_at) "
            "VALUES (?, ?, ?)",
            ("o/r", 3, "2026-01-05T00:00:00Z"),
        )
        # (4) bookmark + inbox overlap
        conn.execute(
            "INSERT INTO inbox (pr_repo, pr_number, title, author_login, "
            "url, is_draft, ci_status, sources, notes, ticket, "
            "pr_updated_at, added_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "o/r", 4, "Both", "carol", "https://gh/o/r/pull/4",
                0, "pending", '["mention"]', "inbox-notes", None,
                "2026-01-06T00:00:00Z", "2026-01-06T00:00:00Z",
                "2026-01-06T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO bookmark (pr_repo, pr_number, title, author_login, "
            "url, state, notes, ticket, bookmarked_at, last_refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "o/r", 4, "Both", "carol", "https://gh/o/r/pull/4",
                "open", "bookmark-notes-win", None,
                "2026-01-07T00:00:00Z", "2026-01-07T00:00:00Z",
            ),
        )
        # (5) worktree + bookmark overlap (worktree row references pr)
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, ticket, "
            "pr_number, pr_repo, pr_author_login, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "myrepo", "wt5", str(tmp_path / "wt5"), "feat/5", None,
                5, "o/r", "dan", "2026-01-08T00:00:00Z", "ready",
            ),
        )
        conn.execute(
            "INSERT INTO bookmark (pr_repo, pr_number, title, author_login, "
            "url, state, notes, ticket, bookmarked_at, last_refreshed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "o/r", 5, "WB", "dan", "https://gh/o/r/pull/5",
                "open", None, None,
                "2026-01-09T00:00:00Z", "2026-01-09T00:00:00Z",
            ),
        )
        # (6) authored_pr_notes-only
        conn.execute(
            "INSERT INTO authored_pr_notes (pr_repo, pr_number, notes, "
            "updated_at) VALUES (?, ?, ?, ?)",
            ("o/r", 6, "authored-only", "2026-01-10T00:00:00Z"),
        )
        # Plus a worktree-only PR (no bookmark / inbox / authored) so
        # the worktree-fold step exercises the WHERE pr_repo IS NOT
        # NULL clause.
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, ticket, "
            "pr_number, pr_repo, pr_author_login, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "myrepo", "wt7", str(tmp_path / "wt7"), "feat/7", None,
                7, "o/r", "eve", "2026-01-11T00:00:00Z", "ready",
            ),
        )
        # Seed one pr_state row keyed (legacy shape) to a worktree —
        # 013's rekey should fold it under (o/r, 5).
        conn.execute(
            "INSERT INTO pr_state (repo, worktree_name, headline, payload, "
            "checked_at) VALUES (?, ?, ?, ?, ?)",
            (
                "myrepo", "wt5", "ready_to_merge",
                '{"pr_number": 5, "url": "https://gh/o/r/pull/5", '
                '"headline": "ready_to_merge"}',
                "2026-01-12T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Now apply 013.
    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        # The surviving rows appear exactly once in pr; the inbox-only
        # PRs (2, 3) were dropped by 014.
        rows = list(conn.execute(
            "SELECT pr_repo, pr_number, is_bookmarked, notes, author_login "
            "FROM pr ORDER BY pr_number"
        ))
        # PR 1 — bookmark-only
        assert rows[0] == ("o/r", 1, 1, "bookmark-notes", "alice")
        # PR 4 — bookmark + (former inbox); bookmark notes win
        assert rows[1] == ("o/r", 4, 1, "bookmark-notes-win", "carol")
        # PR 5 — bookmark + worktree-attached
        assert rows[2] == ("o/r", 5, 1, None, "dan")
        # PR 6 — authored-only (notes only)
        assert rows[3] == ("o/r", 6, 0, "authored-only", None)
        # PR 7 — worktree-only
        assert rows[4] == ("o/r", 7, 0, None, "eve")
        # PR 2 / PR 3 were inbox-only (no bookmark / notes / worktree).
        present = {(r[0], r[1]) for r in rows}
        assert ("o/r", 2) not in present
        assert ("o/r", 3) not in present

        # The inbox/archive columns are gone from pr.
        pr_cols = {r[1] for r in conn.execute("PRAGMA table_info(pr)")}
        assert "is_inbox" not in pr_cols
        assert "is_archived" not in pr_cols
        assert "inbox_sources" not in pr_cols

        # pr_state rekeyed to (pr_repo, pr_number). PR 5 survived 014,
        # so its rekeyed state row survives too.
        ps = conn.execute(
            "SELECT pr_repo, pr_number, headline FROM pr_state"
        ).fetchall()
        assert ps == [("o/r", 5, "ready_to_merge")]

        # worktree.pr_author_login column is gone.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(worktree)")]
        assert "pr_author_login" not in cols

        # The 4 legacy tables are gone.
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "bookmark" not in tables
        assert "inbox" not in tables
        assert "inbox_archived" not in tables
        assert "authored_pr_notes" not in tables

        # pr_state's FK now points at pr (not worktree).
        fks = conn.execute("PRAGMA foreign_key_list(pr_state)").fetchall()
        assert fks, "pr_state should have a FK"
        for fk in fks:
            assert fk[2] == "pr", (
                f"pr_state FK target must be 'pr', got {fk[2]!r}"
            )

        # worktree's new FK to pr is present.
        fks = conn.execute("PRAGMA foreign_key_list(worktree)").fetchall()
        assert fks, "worktree should have a FK to pr"
        for fk in fks:
            assert fk[2] == "pr", (
                f"worktree FK target must be 'pr', got {fk[2]!r}"
            )
    finally:
        conn.close()


def test_migration_013_is_idempotent(db_path: Path) -> None:
    """Re-running ``apply_migrations_sync`` after 013 lands must be a
    no-op — the runner's ``_migration`` table skips already-applied
    migrations. Catches regression where 013's defensive ``DROP TABLE
    IF EXISTS`` would otherwise wipe the new ``pr`` table on a second
    run."""
    db.apply_migrations_sync(db_path)
    # Insert a row to prove it survives a second call.
    conn = db.open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO pr (pr_repo, pr_number, is_bookmarked, "
            "bookmarked_at) VALUES (?, ?, 1, ?)",
            ("o/r", 1, "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    db.apply_migrations_sync(db_path)

    conn = db.open_db(db_path)
    try:
        rows = list(conn.execute(
            "SELECT pr_repo, pr_number, is_bookmarked FROM pr"
        ))
    finally:
        conn.close()
    assert rows == [("o/r", 1, 1)]


def test_migration_013_creates_forced_pre_backup(
    db_path: Path, tmp_path: Path
) -> None:
    """Public users upgrading within the 24h backup window would
    otherwise get no fresh pre-013 snapshot. The forced backup runs
    unconditionally when 013 is pending."""
    # Stop at 012 so 013 is pending on the next apply.
    conn = _apply_through_012(db_path)
    conn.close()
    # Create a recent rolling backup so _backup_if_stale would normally
    # skip — the forced backup must still fire.
    recent_bak = db_path.parent / f"{db_path.name}.bak.recent"
    recent_bak.write_bytes(b"x")

    db.apply_migrations_sync(db_path)

    target = db_path.parent / f"{db_path.name}.bak.pre-013_unified_pr"
    assert target.exists(), "expected forced pre-013 backup file"


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
