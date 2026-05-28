"""pr table row seeder for tests.

Used by tests that need a pr row to exist before they exercise
pr_state inserts (FK to pr) or assert on the unified surface.
"""
from __future__ import annotations

from pathlib import Path

from app.models.pr import PrRow
from app.services import pr_db


def seed_pr(
    db_path: Path,
    *,
    pr_repo: str,
    pr_number: int,
    is_bookmarked: bool = False,
    is_inbox: bool = False,
    is_archived: bool = False,
    bookmarked_at: str | None = None,
    inbox_added_at: str | None = None,
    archived_at: str | None = None,
    inbox_sources: list[str] | None = None,
    title: str | None = None,
    author_login: str | None = None,
    url: str | None = None,
    ticket: str | None = None,
    state: str | None = None,
    is_draft: bool = False,
    ci_status: str | None = None,
    pr_updated_at: str | None = None,
    notes: str | None = None,
    last_seen_at: str | None = None,
    last_refreshed_at: str | None = None,
) -> PrRow:
    """Insert one pr row + return it.

    Defaults to a minimal stub row (no origin flag, no metadata) —
    pass kwargs for the fields a specific test cares about. Round-
    trips through :func:`pr_db.upsert_pr_sync` so the JSON encoding
    of ``inbox_sources`` stays consistent with production writes.
    """
    row = PrRow(
        pr_repo=pr_repo,
        pr_number=pr_number,
        is_bookmarked=is_bookmarked,
        is_inbox=is_inbox,
        is_archived=is_archived,
        bookmarked_at=bookmarked_at,
        inbox_added_at=inbox_added_at,
        archived_at=archived_at,
        inbox_sources=list(inbox_sources) if inbox_sources is not None else [],
        title=title,
        author_login=author_login,
        url=url,
        ticket=ticket,
        state=state,  # type: ignore[arg-type]
        is_draft=is_draft,
        ci_status=ci_status,  # type: ignore[arg-type]
        pr_updated_at=pr_updated_at,
        notes=notes,
        last_seen_at=last_seen_at,
        last_refreshed_at=last_refreshed_at,
    )
    pr_db.upsert_pr_sync(row, db_path=db_path)
    return row
