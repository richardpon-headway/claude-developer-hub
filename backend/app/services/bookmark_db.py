"""Compatibility shim over :mod:`pr_db` for the bookmark surface.

Preserves every public signature from before plan-59 so route + poller
call sites stay untouched. Internally each function projects ``PrRow``
to ``BookmarkRow`` (or upserts a ``PrRow`` with the bookmark fields
set) and routes the work through :mod:`pr_db`.

Plan-61 removes this shim — routes consume ``pr_db`` directly.
"""
from __future__ import annotations

from pathlib import Path

from app.db import get_db_path, open_db
from app.models.bookmark import BookmarkRow
from app.models.pr import PrRow
from app.services import pr_db


class BookmarkExistsError(Exception):
    """Raised by :func:`insert_bookmark_sync` when the pr row already
    has ``is_bookmarked=1``."""


def _to_bookmark_row(pr: PrRow) -> BookmarkRow | None:
    """Project a PrRow to a BookmarkRow when ``is_bookmarked=1``, else
    None. Non-Optional fields on BookmarkRow are sourced from columns
    the bookmark upsert always populates."""
    if not pr.is_bookmarked:
        return None
    return BookmarkRow(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=pr.title or "",
        author_login=pr.author_login or "",
        url=pr.url or "",
        state=pr.state or "open",
        notes=pr.notes,
        ticket=pr.ticket,
        bookmarked_at=pr.bookmarked_at or "",
        last_refreshed_at=pr.last_refreshed_at or pr.bookmarked_at or "",
    )


def list_bookmarks_sync(db_path: Path | None = None) -> list[BookmarkRow]:
    rows = pr_db.list_pr_sync(
        is_bookmarked=True,
        order_by="pr.bookmarked_at DESC",
        db_path=db_path,
    )
    out: list[BookmarkRow] = []
    for pr in rows:
        b = _to_bookmark_row(pr)
        if b is not None:
            out.append(b)
    return out


def get_bookmark_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> BookmarkRow | None:
    pr = pr_db.get_pr_sync(pr_repo, pr_number, db_path=db_path)
    if pr is None:
        return None
    return _to_bookmark_row(pr)


def insert_bookmark_sync(
    row: BookmarkRow, db_path: Path | None = None
) -> None:
    """Insert a new bookmark. Raises :class:`BookmarkExistsError` when
    the pr row already has ``is_bookmarked=1`` (route handlers return
    409). When the pr row exists with other flags but not bookmark,
    we layer the bookmark fields on top — that's the unified model."""
    existing = pr_db.get_pr_sync(row.pr_repo, row.pr_number, db_path=db_path)
    if existing is not None and existing.is_bookmarked:
        raise BookmarkExistsError(
            f"bookmark already exists for {row.pr_repo}#{row.pr_number}"
        )
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo=row.pr_repo,
            pr_number=row.pr_number,
            is_bookmarked=True,
            bookmarked_at=row.bookmarked_at,
            title=row.title,
            author_login=row.author_login,
            url=row.url,
            state=row.state,
            ticket=row.ticket,
            notes=row.notes,
            last_refreshed_at=row.last_refreshed_at,
        ),
        db_path=db_path,
    )


def delete_bookmark_sync(
    pr_repo: str, pr_number: int, db_path: Path | None = None
) -> int:
    """Clear ``is_bookmarked`` + GC the pr row if no other surface
    holds it. Returns 1 if the row was bookmarked (matching the
    legacy ``DELETE FROM bookmark`` rowcount), 0 otherwise."""
    existing = pr_db.get_pr_sync(pr_repo, pr_number, db_path=db_path)
    if existing is None or not existing.is_bookmarked:
        return 0
    pr_db.set_bookmark_flag_sync(pr_repo, pr_number, False, db_path=db_path)
    pr_db.maybe_gc_sync(pr_repo, pr_number, db_path=db_path)
    return 1


def update_bookmark_notes_sync(
    pr_repo: str,
    pr_number: int,
    notes: str,
    db_path: Path | None = None,
) -> int:
    """Overwrite the notes column on a bookmarked row. Returns rowcount."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "UPDATE pr SET notes = ? "
            "WHERE pr_repo = ? AND pr_number = ? AND is_bookmarked = 1",
            (notes, pr_repo, pr_number),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def bookmark_pr_keys_sync(db_path: Path | None = None) -> set[tuple[str, int]]:
    return pr_db.bookmarked_keys_sync(db_path=db_path)
