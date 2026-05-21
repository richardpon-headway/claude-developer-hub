"""SQLite helpers for the ``bookmark`` table.

All functions are sync; async callers wrap with ``asyncio.to_thread``.
Matches the pattern from :mod:`app.services.inbox_db` and
:mod:`app.services.worktree`.

Bookmarks are manually-added PR watches. Lifecycle is symmetric:
add via URL paste, remove via explicit unbookmark. The background
poller refreshes search-driven fields (state, title, author) but
never deletes rows on close / merge.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db import get_db_path, open_db
from app.models.bookmark import BookmarkRow

_LIST_SELECT = (
    "SELECT pr_repo, pr_number, title, author_login, url, state, "
    "       notes, ticket, bookmarked_at, last_refreshed_at "
    "FROM bookmark"
)


def _row_to_model(row: tuple) -> BookmarkRow:
    return BookmarkRow(
        pr_repo=row[0],
        pr_number=row[1],
        title=row[2],
        author_login=row[3],
        url=row[4],
        state=row[5],
        notes=row[6],
        ticket=row[7],
        bookmarked_at=row[8],
        last_refreshed_at=row[9],
    )


def list_bookmarks_sync(db_path: Path | None = None) -> list[BookmarkRow]:
    """Return all bookmarks, newest-bookmarked first."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(f"{_LIST_SELECT} ORDER BY bookmarked_at DESC")
        return [_row_to_model(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_bookmark_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> BookmarkRow | None:
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


class BookmarkExistsError(Exception):
    """Raised by :func:`insert_bookmark_sync` when a row with the same
    ``(pr_repo, pr_number)`` already exists. Route handlers surface this
    as a 409 so the user knows their bookmark already exists rather
    than silently overwriting it."""


def insert_bookmark_sync(
    row: BookmarkRow, db_path: Path | None = None
) -> None:
    """Insert a new bookmark. Raises :class:`BookmarkExistsError` on
    PK conflict (caller should respond 409). Does NOT upsert — the add
    action is explicit and the user should see that the row already
    exists."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        try:
            conn.execute(
                "INSERT INTO bookmark ("
                "  pr_repo, pr_number, title, author_login, url, state, "
                "  notes, ticket, bookmarked_at, last_refreshed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.pr_repo,
                    row.pr_number,
                    row.title,
                    row.author_login,
                    row.url,
                    row.state,
                    row.notes,
                    row.ticket,
                    row.bookmarked_at,
                    row.last_refreshed_at,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise BookmarkExistsError(
                f"bookmark already exists for {row.pr_repo}#{row.pr_number}"
            ) from e
        conn.commit()
    finally:
        conn.close()


def delete_bookmark_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Hard-delete a bookmark row. Returns rows affected (0 = the row
    wasn't there to begin with — the route handler turns that into 404)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM bookmark WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def refresh_bookmark_state_sync(
    pr_repo: str,
    pr_number: int,
    *,
    state: str,
    title: str,
    author_login: str,
    ticket: str | None,
    last_refreshed_at: str,
    db_path: Path | None = None,
) -> int:
    """Refresh the search-driven fields on a bookmark from a
    ``gh pr view`` probe. Leaves user-owned fields (``notes``,
    ``bookmarked_at``) alone. Returns rows affected (0 = the row
    disappeared between list_bookmarks and the refresh write, which
    can happen if the user clicked Unbookmark mid-tick)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE bookmark SET "
            "  state = ?, title = ?, author_login = ?, ticket = ?, "
            "  last_refreshed_at = ? "
            "WHERE pr_repo = ? AND pr_number = ?",
            (state, title, author_login, ticket, last_refreshed_at,
             pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_bookmark_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    db_path: Path | None = None,
) -> int:
    """Overwrite the notes column. Empty string is valid (clears).
    Returns rows affected (0 = row not found — caller returns 404)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE bookmark SET notes = ? WHERE pr_repo = ? AND pr_number = ?",
            (notes, pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def bookmark_pr_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    """All ``(pr_repo, pr_number)`` currently bookmarked. Used by the
    inbox poller's dedup logic so an inbox row and a bookmark for the
    same PR don't both render — bookmarks win (they're explicit)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute("SELECT pr_repo, pr_number FROM bookmark")
        return {(row[0], row[1]) for row in cur.fetchall()}
    finally:
        conn.close()
