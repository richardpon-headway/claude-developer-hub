"""Compatibility shim over :mod:`pr_db` for the inbox surface.

Preserves every public signature from before plan-59. Internally
delegates to :mod:`pr_db`; the legacy ``inbox`` + ``inbox_archived``
tables no longer exist (migration 013 folded both into ``pr``).

Plan-61 removes this shim — routes + the poll loop consume ``pr_db``
directly.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.db import get_db_path, open_db
from app.models.inbox import InboxRow
from app.services import pr_db


def _to_inbox_row(pr: pr_db.PrRow) -> InboxRow | None:
    """Project a PrRow to an InboxRow when ``is_inbox=1``, else None."""
    if not pr.is_inbox:
        return None
    return InboxRow(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=pr.title or "",
        author_login=pr.author_login or "",
        url=pr.url or "",
        is_draft=pr.is_draft,
        ci_status=pr.ci_status or "none",  # type: ignore[arg-type]
        sources=list(pr.inbox_sources),
        notes=pr.notes,
        ticket=pr.ticket,
        pr_updated_at=pr.pr_updated_at or "",
        added_at=pr.inbox_added_at or "",
        last_seen_at=pr.last_seen_at or "",
    )


def list_inbox_sync(db_path: Path | None = None) -> list[InboxRow]:
    """Inbox rows that aren't archived, newest first."""
    rows = pr_db.list_pr_sync(
        is_inbox=True,
        is_archived=False,
        order_by="pr.pr_updated_at DESC",
        db_path=db_path,
    )
    out: list[InboxRow] = []
    for pr in rows:
        ib = _to_inbox_row(pr)
        if ib is not None:
            out.append(ib)
    return out


def get_inbox_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> InboxRow | None:
    """Fetch one inbox row regardless of archive state."""
    pr = pr_db.get_pr_sync(pr_repo, pr_number, db_path=db_path)
    if pr is None:
        return None
    return _to_inbox_row(pr)


def upsert_inbox_sync(row: InboxRow, db_path: Path | None = None) -> None:
    """Insert or refresh an inbox row.

    On insert: all fields written. On conflict: refresh the search-
    driven fields (title/author/url/is_draft/ci_status/sources/
    pr_updated_at/last_seen_at) and leave user-owned + first-seen
    fields (notes, added_at) untouched. Matches the legacy contract.
    """
    if db_path is None:
        db_path = get_db_path()
    sources_json = json.dumps(list(row.sources))
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO pr ("
            "  pr_repo, pr_number, is_inbox, inbox_added_at, inbox_sources, "
            "  title, author_login, url, is_draft, ci_status, notes, ticket, "
            "  pr_updated_at, last_seen_at"
            ") VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pr_repo, pr_number) DO UPDATE SET "
            "  is_inbox       = 1, "
            "  title          = excluded.title, "
            "  author_login   = excluded.author_login, "
            "  url            = excluded.url, "
            "  is_draft       = excluded.is_draft, "
            "  ci_status      = excluded.ci_status, "
            "  inbox_sources  = excluded.inbox_sources, "
            "  ticket         = COALESCE(excluded.ticket, pr.ticket), "
            "  pr_updated_at  = excluded.pr_updated_at, "
            "  last_seen_at   = excluded.last_seen_at",
            (
                row.pr_repo,
                row.pr_number,
                row.added_at,
                sources_json,
                row.title,
                row.author_login,
                row.url,
                1 if row.is_draft else 0,
                row.ci_status,
                row.notes,
                row.ticket,
                row.pr_updated_at,
                row.last_seen_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_inbox_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Clear inbox + archive flags then GC the pr row if no other
    surface holds it. Returns total state changes (matches the legacy
    "rowcount across both inbox and inbox_archived" tally)."""
    existing = pr_db.get_pr_sync(pr_repo, pr_number, db_path=db_path)
    if existing is None:
        return 0
    changes = 0
    if existing.is_inbox:
        pr_db.set_inbox_flag_sync(pr_repo, pr_number, False, db_path=db_path)
        changes += 1
    if existing.is_archived:
        pr_db.set_archived_flag_sync(
            pr_repo, pr_number, False, db_path=db_path
        )
        changes += 1
    if changes:
        pr_db.maybe_gc_sync(pr_repo, pr_number, db_path=db_path)
    return changes


def archive_inbox_sync(
    pr_repo: str,
    pr_number: int,
    archived_at: str,
    db_path: Path | None = None,
) -> None:
    """Sticky-dismiss the PR. Idempotent: a second archive preserves
    the original ``archived_at`` (matches the legacy ``INSERT OR
    IGNORE INTO inbox_archived`` semantic).

    Creates the pr row if it doesn't already exist — the legacy
    ``inbox_archived`` table was a standalone presence marker keyed
    independently of ``inbox``. After unification we keep that
    standalone contract: archiving works even when no inbox row has
    been written yet (e.g., the user archives by URL via a future
    direct-archive endpoint, or the dedup-by-archive path fires
    before the first poll tick).
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO pr (pr_repo, pr_number, is_archived, archived_at) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(pr_repo, pr_number) DO UPDATE SET "
            "  is_archived = 1, "
            "  archived_at = COALESCE(pr.archived_at, excluded.archived_at)",
            (pr_repo, pr_number, archived_at),
        )
        conn.commit()
    finally:
        conn.close()


def archived_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    return pr_db.archived_keys_sync(db_path=db_path)


def update_inbox_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    db_path: Path | None = None,
) -> int:
    """Overwrite notes on an inbox-flagged row. Returns rowcount."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE pr SET notes = ? "
            "WHERE pr_repo = ? AND pr_number = ? AND is_inbox = 1",
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
    return pr_db.list_stale_inbox_sync(cutoff, limit, db_path=db_path)


def inbox_pr_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    return pr_db.inbox_keys_sync(db_path=db_path)


def touch_last_seen_sync(
    pr_repo: str,
    pr_number: int,
    last_seen_at: str,
    db_path: Path | None = None,
) -> None:
    """Bump ``last_seen_at`` on one inbox-flagged row. Wraps the bulk
    :func:`pr_db.touch_last_seen_sync` for the per-row caller in
    :mod:`inbox_poll`. (Plan-60 will switch inbox_poll to the bulk
    form directly; this single-pair shim keeps the call site untouched
    in phase 1.)"""
    pr_db.touch_last_seen_sync(
        [(pr_repo, pr_number)], last_seen_at, db_path=db_path
    )
