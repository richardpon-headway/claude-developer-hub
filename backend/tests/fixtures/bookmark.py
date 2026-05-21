"""Bookmark builders for tests."""
from __future__ import annotations

from pathlib import Path

from app.models.bookmark import BookmarkRow, BookmarkState
from app.services import bookmark_db


def build_bookmark_row(
    *,
    pr_repo: str = "o/r",
    pr_number: int = 1,
    title: str | None = None,
    author_login: str = "me",
    url: str | None = None,
    state: BookmarkState = "open",
    notes: str | None = None,
    ticket: str | None = None,
    bookmarked_at: str = "2026-05-21T00:00:00Z",
    last_refreshed_at: str = "2026-05-21T00:00:00Z",
) -> BookmarkRow:
    return BookmarkRow(
        pr_repo=pr_repo,
        pr_number=pr_number,
        title=title or f"PR #{pr_number}",
        author_login=author_login,
        url=url or f"https://github.com/{pr_repo}/pull/{pr_number}",
        state=state,
        notes=notes,
        ticket=ticket,
        bookmarked_at=bookmarked_at,
        last_refreshed_at=last_refreshed_at,
    )


def seed_bookmark(db_path: Path, **kwargs: object) -> BookmarkRow:
    row = build_bookmark_row(**kwargs)  # type: ignore[arg-type]
    bookmark_db.insert_bookmark_sync(row, db_path=db_path)
    return row
