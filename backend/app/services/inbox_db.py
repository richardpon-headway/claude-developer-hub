"""SQLite helpers for the ``inbox`` and ``inbox_archived`` tables.

All functions are sync (sqlite3 is sync-only); async callers wrap with
``asyncio.to_thread``. Matches the pattern from
:mod:`app.services.worktree`.

The persistent-inbox model means inbox rows live in SQLite from the
moment they're discovered via ``gh search prs`` until either the PR
closes / merges (auto-removal sweep) or the user explicitly archives
them. Archive is recorded in a separate ``inbox_archived`` table; the
list query joins them out via ``NOT IN`` so archive is reversible
without losing the inbox row's PR metadata.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.db import get_db_path, open_db
from app.models.inbox import InboxRow

_LIST_SELECT = (
    "SELECT pr_repo, pr_number, title, author_login, url, is_draft, "
    "       ci_status, sources, notes, ticket, pr_updated_at, "
    "       added_at, last_seen_at "
    "FROM inbox"
)


def _row_to_model(row: tuple) -> InboxRow:
    return InboxRow(
        pr_repo=row[0],
        pr_number=row[1],
        title=row[2],
        author_login=row[3],
        url=row[4],
        is_draft=bool(row[5]),
        ci_status=row[6],
        sources=json.loads(row[7]),
        notes=row[8],
        ticket=row[9],
        pr_updated_at=row[10],
        added_at=row[11],
        last_seen_at=row[12],
    )


def list_inbox_sync(db_path: Path | None = None) -> list[InboxRow]:
    """Return inbox rows that haven't been archived, newest first."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            f"{_LIST_SELECT} "
            "WHERE (pr_repo, pr_number) NOT IN "
            "  (SELECT pr_repo, pr_number FROM inbox_archived) "
            "ORDER BY pr_updated_at DESC"
        )
        return [_row_to_model(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_inbox_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> InboxRow | None:
    """Fetch one row, regardless of archive state. Pull-down + notes
    update both need the row even when the user has archived it (the
    UI shouldn't be able to surface an archived row, but the DB
    contract is independent of view filtering)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            f"{_LIST_SELECT} WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        row = cur.fetchone()
        return _row_to_model(row) if row else None
    finally:
        conn.close()


def upsert_inbox_sync(row: InboxRow, db_path: Path | None = None) -> None:
    """Insert or refresh an inbox row.

    On insert, all fields are stored as supplied.
    On conflict, refresh the search-driven fields (title, author,
    url, is_draft, ci_status, sources, ticket, pr_updated_at,
    last_seen_at) and leave the user-owned + first-seen fields
    untouched (``notes``, ``added_at``).
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO inbox ("
            "  pr_repo, pr_number, title, author_login, url, is_draft, "
            "  ci_status, sources, notes, ticket, pr_updated_at, "
            "  added_at, last_seen_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pr_repo, pr_number) DO UPDATE SET "
            "  title = excluded.title, "
            "  author_login = excluded.author_login, "
            "  url = excluded.url, "
            "  is_draft = excluded.is_draft, "
            "  ci_status = excluded.ci_status, "
            "  sources = excluded.sources, "
            "  ticket = excluded.ticket, "
            "  pr_updated_at = excluded.pr_updated_at, "
            "  last_seen_at = excluded.last_seen_at",
            (
                row.pr_repo,
                row.pr_number,
                row.title,
                row.author_login,
                row.url,
                1 if row.is_draft else 0,
                row.ci_status,
                json.dumps(row.sources),
                row.notes,
                row.ticket,
                row.pr_updated_at,
                row.added_at,
                row.last_seen_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_inbox_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Hard-delete the inbox row + its (archived?) shadow.

    Used by the auto-removal sweep when ``gh pr view`` reports a
    PR's state has transitioned to closed or merged. Also deletes
    the matching ``inbox_archived`` row so the slot is fully cleared
    — a future PR that happens to share the same number would
    otherwise be silently filtered out. Returns total rows
    affected across both tables.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur1 = conn.execute(
            "DELETE FROM inbox WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        cur2 = conn.execute(
            "DELETE FROM inbox_archived WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        conn.commit()
        return cur1.rowcount + cur2.rowcount
    finally:
        conn.close()


def archive_inbox_sync(
    pr_repo: str,
    pr_number: int,
    archived_at: str,
    db_path: Path | None = None,
) -> None:
    """Record a user-initiated dismissal. Idempotent: a second archive
    on the same (pr_repo, pr_number) is a no-op (INSERT OR IGNORE).

    Does not touch the ``inbox`` row itself — the row stays so its
    metadata can keep refreshing on subsequent ticks (preserving
    notes, for instance, in case the user later un-archives).
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO inbox_archived "
            "(pr_repo, pr_number, archived_at) VALUES (?, ?, ?)",
            (pr_repo, pr_number, archived_at),
        )
        conn.commit()
    finally:
        conn.close()


def archived_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    """Set of all archived ``(pr_repo, pr_number)`` pairs. Used by the
    poll tick to skip re-upserts for rows the user has dismissed."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute("SELECT pr_repo, pr_number FROM inbox_archived")
        return {(row[0], row[1]) for row in cur.fetchall()}
    finally:
        conn.close()


def update_inbox_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    db_path: Path | None = None,
) -> int:
    """Overwrite the notes column. Empty string is a valid value (it
    clears the note). Returns the row count actually updated — 0 means
    the (pr_repo, pr_number) wasn't in the inbox table."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE inbox SET notes = ? WHERE pr_repo = ? AND pr_number = ?",
            (notes, pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def list_stale_inbox_sync(
    cutoff: str,
    limit: int,
    db_path: Path | None = None,
) -> list[tuple[str, int]]:
    """Return up to ``limit`` ``(pr_repo, pr_number)`` pairs whose
    ``last_seen_at`` is strictly older than ``cutoff``, oldest first.

    The auto-removal sweep uses this to bound how many ``gh pr view``
    calls fire per tick — a long-stale PR doesn't need probing every
    60s, but it does need probing eventually to catch close / merge.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number FROM inbox "
            "WHERE last_seen_at < ? "
            "ORDER BY last_seen_at ASC "
            "LIMIT ?",
            (cutoff, limit),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]
    finally:
        conn.close()


def inbox_pr_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    """All ``(pr_repo, pr_number)`` currently in the inbox table
    (archived or not). Used by the worktree dedup logic — a PR with
    a local worktree shouldn't also appear in the inbox."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute("SELECT pr_repo, pr_number FROM inbox")
        return {(row[0], row[1]) for row in cur.fetchall()}
    finally:
        conn.close()
