"""SQLite helpers for the ``authored_pr_notes`` table.

Used by the "My PRs (no worktree)" tier on the hub. The tier itself
is recomputed each request from ``gh search prs --author:@me``, so
notes need their own tiny persistence layer keyed on the PR
identifiers. When the user pulls the row down or bookmarks it, the
route handler copies the notes to the destination surface and
deletes the row here — see plan-50 "Notes migration on surface
transition".

All functions are sync; async callers wrap with ``asyncio.to_thread``.
"""
from __future__ import annotations

from pathlib import Path

from app.db import get_db_path, open_db


def get_notes_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> str | None:
    """Return the notes string for one PR, or ``None`` if no row."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT notes FROM authored_pr_notes "
            "WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def upsert_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    updated_at: str,
    db_path: Path | None = None,
) -> None:
    """Insert or overwrite the notes for one PR.

    Empty string is a valid value (clears the visible note); we still
    keep the row so the slot is tracked and surface-transition logic
    knows to migrate it. If the user wants the row gone, the
    appropriate path is to bookmark or pull down the PR — which moves
    the notes to that surface and deletes here."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO authored_pr_notes "
            "(pr_repo, pr_number, notes, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pr_repo, pr_number) DO UPDATE SET "
            "  notes = excluded.notes, "
            "  updated_at = excluded.updated_at",
            (pr_repo, pr_number, notes, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def delete_notes_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Drop the notes row. Called by the pull-down + bookmark-add
    handlers when an authored-tier row transitions to a worktree or
    bookmark — the destination surface's own notes column takes over."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM authored_pr_notes "
            "WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def notes_by_keys_sync(
    keys: set[tuple[str, int]], db_path: Path | None = None
) -> dict[tuple[str, int], str]:
    """Batch lookup for a list of ``(pr_repo, pr_number)`` pairs.
    Used by the authored-PR fetch path to attach notes to each row in
    a single query rather than N round-trips."""
    if not keys:
        return {}
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        # SQLite has no native tuple-IN. Use a parameterized OR chain;
        # ``len(keys)`` is bounded by `gh search --limit=100` so the
        # statement stays compact.
        placeholders = " OR ".join(
            "(pr_repo = ? AND pr_number = ?)" for _ in keys
        )
        params: list = []
        for pr_repo, pr_number in keys:
            params.extend([pr_repo, pr_number])
        cur = conn.execute(
            "SELECT pr_repo, pr_number, notes FROM authored_pr_notes "
            f"WHERE {placeholders}",
            params,
        )
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}
    finally:
        conn.close()
