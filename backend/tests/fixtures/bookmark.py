"""Bookmark seeders for tests — thin wrappers over :func:`seed_pr`."""
from __future__ import annotations

from pathlib import Path

from app.models.pr import PrRow
from tests.fixtures.pr import seed_pr


def seed_bookmark(
    db_path: Path,
    *,
    pr_repo: str = "o/r",
    pr_number: int = 1,
    title: str | None = None,
    author_login: str = "me",
    url: str | None = None,
    state: str = "open",
    notes: str | None = None,
    ticket: str | None = None,
    bookmarked_at: str = "2026-05-21T00:00:00Z",
    last_refreshed_at: str = "2026-05-21T00:00:00Z",
) -> PrRow:
    """Insert one bookmarked pr row + return it."""
    return seed_pr(
        db_path,
        pr_repo=pr_repo,
        pr_number=pr_number,
        is_bookmarked=True,
        bookmarked_at=bookmarked_at,
        title=title or f"PR #{pr_number}",
        author_login=author_login,
        url=url or f"https://github.com/{pr_repo}/pull/{pr_number}",
        state=state,
        notes=notes,
        ticket=ticket,
        last_refreshed_at=last_refreshed_at,
    )
