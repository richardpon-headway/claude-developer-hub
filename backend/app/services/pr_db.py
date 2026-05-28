"""SQLite helpers for the unified ``pr`` table.

Owns every read and write of the ``pr`` table created by migration
013. Sync (sqlite3 is sync-only); async callers wrap with
``asyncio.to_thread``.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.db import get_db_path, open_db
from app.models.pr import PrRow
from app.models.worktree import PrStateSummary

# All scalar columns on the pr table, in canonical positional order.
# Used by both _LIST_SELECT and _row_to_model so the unpacking stays
# aligned.
_BASE_COLS = (
    "pr.pr_repo, pr.pr_number, pr.is_bookmarked, pr.is_inbox, "
    "pr.is_archived, pr.bookmarked_at, pr.inbox_added_at, pr.archived_at, "
    "pr.inbox_sources, pr.title, pr.author_login, pr.url, pr.ticket, "
    "pr.state, pr.is_draft, pr.ci_status, pr.pr_updated_at, pr.notes, "
    "pr.last_seen_at, pr.last_refreshed_at"
)

# Join pr_state so callers can render the rich payload without a
# second round-trip. Mirrors worktree._LIST_SELECT's shape.
_LIST_SELECT = (
    f"SELECT {_BASE_COLS}, p.payload, p.checked_at "
    "FROM pr "
    "LEFT JOIN pr_state p "
    "  ON p.pr_repo = pr.pr_repo AND p.pr_number = pr.pr_number"
)


def _row_to_model(row: tuple) -> PrRow:
    (
        pr_repo, pr_number, is_bookmarked, is_inbox, is_archived,
        bookmarked_at, inbox_added_at, archived_at, inbox_sources,
        title, author_login, url, ticket, state, is_draft, ci_status,
        pr_updated_at, notes, last_seen_at, last_refreshed_at,
        payload_json, checked_at,
    ) = row

    sources: list[str] = []
    if inbox_sources:
        try:
            parsed = json.loads(inbox_sources)
            if isinstance(parsed, list):
                sources = [str(s) for s in parsed]
        except (TypeError, ValueError):
            sources = []

    pr_state: PrStateSummary | None = None
    if payload_json is not None and checked_at is not None:
        try:
            data = json.loads(payload_json)
            data["checked_at"] = checked_at
            # Back-compat with payloads written before the multi-label
            # change — fall back to a single-element labels list.
            if "labels" not in data:
                data["labels"] = (
                    [data["headline"]] if data.get("headline") else []
                )
            pr_state = PrStateSummary.model_validate(data)
        except Exception:
            pr_state = None

    return PrRow(
        pr_repo=pr_repo,
        pr_number=pr_number,
        is_bookmarked=bool(is_bookmarked),
        is_inbox=bool(is_inbox),
        is_archived=bool(is_archived),
        bookmarked_at=bookmarked_at,
        inbox_added_at=inbox_added_at,
        archived_at=archived_at,
        inbox_sources=sources,
        title=title,
        author_login=author_login,
        url=url,
        ticket=ticket,
        state=state,
        is_draft=bool(is_draft),
        ci_status=ci_status,
        pr_updated_at=pr_updated_at,
        notes=notes,
        last_seen_at=last_seen_at,
        last_refreshed_at=last_refreshed_at,
        pr_state=pr_state,
    )


def upsert_pr_sync(row: PrRow, db_path: Path | None = None) -> None:
    """Insert or update a pr row.

    Origin booleans use ``MAX(pr.flag, excluded.flag)`` so a discovery
    poller setting ``is_inbox=True`` doesn't wipe an earlier
    ``is_bookmarked=True``. Scalar fields use COALESCE-on-excluded so
    a write that doesn't carry a value (e.g., bookmark upsert without
    is_draft info) doesn't blank out what a prior source set.

    ``is_draft`` is the exception — it's a latest-reading-wins boolean
    rather than a sticky flag, so we always take ``excluded.is_draft``.
    """
    if db_path is None:
        db_path = get_db_path()
    sources_json = json.dumps(list(row.inbox_sources)) if row.inbox_sources else None
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO pr ("
            "  pr_repo, pr_number, is_bookmarked, is_inbox, is_archived, "
            "  bookmarked_at, inbox_added_at, archived_at, inbox_sources, "
            "  title, author_login, url, ticket, state, is_draft, ci_status, "
            "  pr_updated_at, notes, last_seen_at, last_refreshed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pr_repo, pr_number) DO UPDATE SET "
            "  is_bookmarked     = MAX(pr.is_bookmarked, excluded.is_bookmarked), "
            "  is_inbox          = MAX(pr.is_inbox, excluded.is_inbox), "
            "  is_archived       = MAX(pr.is_archived, excluded.is_archived), "
            "  bookmarked_at     = COALESCE(excluded.bookmarked_at, pr.bookmarked_at), "
            "  inbox_added_at    = COALESCE(excluded.inbox_added_at, pr.inbox_added_at), "
            "  archived_at       = COALESCE(excluded.archived_at, pr.archived_at), "
            "  inbox_sources     = COALESCE(excluded.inbox_sources, pr.inbox_sources), "
            "  title             = COALESCE(excluded.title, pr.title), "
            "  author_login      = COALESCE(excluded.author_login, pr.author_login), "
            "  url               = COALESCE(excluded.url, pr.url), "
            "  ticket            = COALESCE(excluded.ticket, pr.ticket), "
            "  state             = COALESCE(excluded.state, pr.state), "
            "  is_draft          = excluded.is_draft, "
            "  ci_status         = COALESCE(excluded.ci_status, pr.ci_status), "
            "  pr_updated_at     = COALESCE(excluded.pr_updated_at, pr.pr_updated_at), "
            "  notes             = COALESCE(excluded.notes, pr.notes), "
            "  last_seen_at      = COALESCE(excluded.last_seen_at, pr.last_seen_at), "
            "  last_refreshed_at = COALESCE(excluded.last_refreshed_at, pr.last_refreshed_at)",
            (
                row.pr_repo, row.pr_number,
                1 if row.is_bookmarked else 0,
                1 if row.is_inbox else 0,
                1 if row.is_archived else 0,
                row.bookmarked_at, row.inbox_added_at, row.archived_at,
                sources_json,
                row.title, row.author_login, row.url, row.ticket,
                row.state,
                1 if row.is_draft else 0,
                row.ci_status, row.pr_updated_at, row.notes,
                row.last_seen_at, row.last_refreshed_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_pr_sync(
    *,
    is_bookmarked: bool | None = None,
    is_inbox: bool | None = None,
    is_archived: bool | None = None,
    has_worktree: bool | None = None,
    author_login: str | None = None,
    state: str | None = None,
    order_by: str | None = None,
    db_path: Path | None = None,
) -> list[PrRow]:
    """Filtered list of pr rows. Each kwarg adds an AND clause to the
    WHERE; ``has_worktree`` uses an EXISTS subquery against worktree.
    ``order_by`` is a SQL fragment chosen by the caller (the shims
    pick the right ordering for their surface).
    """
    if db_path is None:
        db_path = get_db_path()

    clauses: list[str] = []
    params: list = []
    if is_bookmarked is not None:
        clauses.append("pr.is_bookmarked = ?")
        params.append(1 if is_bookmarked else 0)
    if is_inbox is not None:
        clauses.append("pr.is_inbox = ?")
        params.append(1 if is_inbox else 0)
    if is_archived is not None:
        clauses.append("pr.is_archived = ?")
        params.append(1 if is_archived else 0)
    if has_worktree is not None:
        op = "EXISTS" if has_worktree else "NOT EXISTS"
        clauses.append(
            f"{op} (SELECT 1 FROM worktree w "
            "WHERE w.pr_repo = pr.pr_repo AND w.pr_number = pr.pr_number)"
        )
    if author_login is not None:
        clauses.append("pr.author_login = ?")
        params.append(author_login)
    if state is not None:
        clauses.append("pr.state = ?")
        params.append(state)

    sql = _LIST_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if order_by:
        sql += f" ORDER BY {order_by}"

    conn = open_db(db_path)
    try:
        cur = conn.execute(sql, params)
        return [_row_to_model(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_pr_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> PrRow | None:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            f"{_LIST_SELECT} WHERE pr.pr_repo = ? AND pr.pr_number = ?",
            (pr_repo, pr_number),
        )
        row = cur.fetchone()
        return _row_to_model(row) if row else None
    finally:
        conn.close()


def delete_pr_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Hard-delete a pr row. Prefer :func:`maybe_gc_sync` for the
    flag-clearing path — this is for tests and explicit teardown.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM pr WHERE pr_repo = ? AND pr_number = ?",
            (pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str | None,
    db_path: Path | None = None,
) -> int:
    """Overwrite the notes column on a pr row. Returns rowcount (0
    means no matching row)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE pr SET notes = ? WHERE pr_repo = ? AND pr_number = ?",
            (notes, pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def upsert_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    updated_at: str,
    db_path: Path | None = None,
) -> None:
    """Set notes on a pr row, inserting a stub row if none exists.

    Used by the authored-PR notes endpoint: the user can type a note
    on an authored PR before the next discovery poll has written its
    pr row, and the note must survive. Empty string is valid (clears
    the visible note). The direct UPDATE path is preferred over
    :func:`upsert_pr_sync` so an explicit clear-to-empty isn't
    COALESCEd back to the previous value.
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


def set_bookmark_flag_sync(
    pr_repo: str,
    pr_number: int,
    value: bool,
    *,
    bookmarked_at: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Toggle the ``is_bookmarked`` flag. When clearing, ``bookmarked_at``
    is preserved (cheap audit trail — re-bookmarking later restores
    the original timestamp via COALESCE-on-upsert)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        if value:
            cur = conn.execute(
                "UPDATE pr SET is_bookmarked = 1, "
                "  bookmarked_at = COALESCE(?, bookmarked_at) "
                "WHERE pr_repo = ? AND pr_number = ?",
                (bookmarked_at, pr_repo, pr_number),
            )
        else:
            cur = conn.execute(
                "UPDATE pr SET is_bookmarked = 0 "
                "WHERE pr_repo = ? AND pr_number = ?",
                (pr_repo, pr_number),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_inbox_flag_sync(
    pr_repo: str,
    pr_number: int,
    value: bool,
    db_path: Path | None = None,
) -> int:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE pr SET is_inbox = ? WHERE pr_repo = ? AND pr_number = ?",
            (1 if value else 0, pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_archived_flag_sync(
    pr_repo: str,
    pr_number: int,
    value: bool,
    *,
    archived_at: str | None = None,
    db_path: Path | None = None,
) -> int:
    """Toggle the ``is_archived`` flag. When setting, ``archived_at``
    only writes if the column is currently NULL — matches the legacy
    ``INSERT OR IGNORE INTO inbox_archived`` idempotency contract."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        if value:
            cur = conn.execute(
                "UPDATE pr SET is_archived = 1, "
                "  archived_at = COALESCE(archived_at, ?) "
                "WHERE pr_repo = ? AND pr_number = ?",
                (archived_at, pr_repo, pr_number),
            )
        else:
            cur = conn.execute(
                "UPDATE pr SET is_archived = 0 "
                "WHERE pr_repo = ? AND pr_number = ?",
                (pr_repo, pr_number),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def touch_last_seen_sync(
    pk_pairs: list[tuple[str, int]],
    last_seen_at: str,
    db_path: Path | None = None,
) -> int:
    """Bulk-bump ``last_seen_at`` for a list of ``(pr_repo, pr_number)``
    pairs. Returns total rowcount across all updates."""
    if not pk_pairs:
        return 0
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.executemany(
            "UPDATE pr SET last_seen_at = ? "
            "WHERE pr_repo = ? AND pr_number = ?",
            [(last_seen_at, r, n) for r, n in pk_pairs],
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def maybe_gc_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Delete the pr row IF no origin flag is set AND notes is NULL
    AND no worktree row references it. Returns 1 if deleted.

    Called after a shim clears the flag(s) it owns — the row evaporates
    once nothing holds it. Worktree-attached rows survive because the
    worktree's ``ON DELETE SET NULL`` would otherwise silently null the
    worktree's PR linkage on a transient empty-flag state.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM pr "
            "WHERE pr_repo = ? AND pr_number = ? "
            "  AND is_bookmarked = 0 AND is_inbox = 0 AND is_archived = 0 "
            "  AND notes IS NULL "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM worktree w "
            "    WHERE w.pr_repo = pr.pr_repo AND w.pr_number = pr.pr_number"
            "  )",
            (pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def bookmarked_keys_sync(
    db_path: Path | None = None,
) -> set[tuple[str, int]]:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number FROM pr WHERE is_bookmarked = 1"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def inbox_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    """All ``(pr_repo, pr_number)`` flagged ``is_inbox=1`` (archived or
    not). Used by the worktree-dedup logic."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number FROM pr WHERE is_inbox = 1"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def archived_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number FROM pr WHERE is_archived = 1"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def worktree_attached_keys_sync(
    db_path: Path | None = None,
) -> set[tuple[str, int]]:
    """All ``(pr_repo, pr_number)`` currently attached to a worktree.

    Computed from the ``worktree`` table (not ``pr``) because a worktree
    can be transitionally PR-linked before a matching pr row exists —
    the linkage is the source of truth.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number FROM worktree "
            "WHERE pr_repo IS NOT NULL AND pr_number IS NOT NULL"
        )
        return {(r[0], r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def list_stale_inbox_sync(
    cutoff: str,
    limit: int,
    db_path: Path | None = None,
) -> list[tuple[str, int]]:
    """Inbox-flagged rows whose ``last_seen_at`` is strictly older than
    ``cutoff``, oldest first, up to ``limit``."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number FROM pr "
            "WHERE is_inbox = 1 AND last_seen_at < ? "
            "ORDER BY last_seen_at ASC "
            "LIMIT ?",
            (cutoff, limit),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()
