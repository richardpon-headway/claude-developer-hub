"""Compatibility shim over :mod:`pr_db` for the authored-PR notes
surface.

Preserves every public signature from before plan-59. The legacy
``authored_pr_notes`` table is gone; notes live on the unified ``pr``
table. Plan-61 inlines these calls into the routes and deletes the
shim.

Semantic preservation: a delete here only clears notes when no other
surface owns the row, so the bookmark / inbox flows that copy authored
notes into the destination via the shim → upsert path don't end up
re-deleting the same column.
"""
from __future__ import annotations

from pathlib import Path

from app.db import get_db_path, open_db
from app.services import pr_db


def get_notes_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> str | None:
    """Return the notes attached to the authored-PR surface for one
    PR, or ``None``.

    Scoped to rows with no origin flag set — the legacy
    ``authored_pr_notes`` table was independent of bookmark/inbox
    storage, so a bookmark's notes never surfaced through this
    helper. After unification we preserve that contract by only
    returning notes when no other surface owns the row.
    """
    pr = pr_db.get_pr_sync(pr_repo, pr_number, db_path=db_path)
    if pr is None:
        return None
    if pr.is_bookmarked or pr.is_inbox or pr.is_archived:
        return None
    return pr.notes


def upsert_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    updated_at: str,
    db_path: Path | None = None,
) -> None:
    """Insert or overwrite notes for one PR.

    Empty string is a valid value (clears the visible note). The
    direct UPDATE path is preferred over ``pr_db.upsert_pr_sync`` so
    an explicit clear-to-empty isn't COALESCEd back to the previous
    value.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE pr SET notes = ?, last_refreshed_at = ? "
            "WHERE pr_repo = ? AND pr_number = ?",
            (notes, updated_at, pr_repo, pr_number),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO pr (pr_repo, pr_number, notes, last_refreshed_at) "
                "VALUES (?, ?, ?, ?)",
                (pr_repo, pr_number, notes, updated_at),
            )
        conn.commit()
    finally:
        conn.close()


def delete_notes_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Clear authored-only notes on the pr row.

    No-op when any origin flag (bookmark / inbox / archive) owns the
    row. Reason: the legacy ``authored_pr_notes`` table was independent
    of bookmark/inbox notes columns, so deleting authored notes never
    affected another surface. Under the unified ``pr.notes`` column we
    preserve that contract by only clearing when no other surface
    holds the row.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE pr SET notes = NULL "
            "WHERE pr_repo = ? AND pr_number = ? "
            "  AND notes IS NOT NULL "
            "  AND is_bookmarked = 0 AND is_inbox = 0 AND is_archived = 0",
            (pr_repo, pr_number),
        )
        conn.commit()
        rowcount = cur.rowcount
    finally:
        conn.close()
    if rowcount:
        pr_db.maybe_gc_sync(pr_repo, pr_number, db_path=db_path)
    return rowcount


def notes_by_keys_sync(
    keys: set[tuple[str, int]], db_path: Path | None = None
) -> dict[tuple[str, int], str]:
    """Batch lookup of ``(pr_repo, pr_number) → notes`` for a set of
    keys. Skips rows whose notes are NULL."""
    if not keys:
        return {}
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        placeholders = " OR ".join(
            "(pr_repo = ? AND pr_number = ?)" for _ in keys
        )
        params: list = []
        for pr_repo, pr_number in keys:
            params.extend([pr_repo, pr_number])
        cur = conn.execute(
            "SELECT pr_repo, pr_number, notes FROM pr "
            f"WHERE ({placeholders}) AND notes IS NOT NULL",
            params,
        )
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}
    finally:
        conn.close()
