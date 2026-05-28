"""Inbox builders + seeders for tests.

``build_raw_pr`` constructs the search-layer shape (``InboxPrRaw``)
that the poll tick consumes. ``seed_inbox_row`` is a thin wrapper
over :func:`seed_pr` for tests that need a persisted inbox-flagged
pr row.
"""
from __future__ import annotations

from pathlib import Path

from app.models.pr import PrRow
from app.services.inbox_search import InboxPrRaw
from tests.fixtures.pr import seed_pr


def build_raw_pr(
    *,
    repo: str = "o/r",
    number: int = 1,
    head: str = "feat/x",
    base: str = "main",
    source: str = "reviewer",
    title: str | None = None,
) -> InboxPrRaw:
    """Build an ``InboxPrRaw`` — the search-layer shape before the
    poll tick maps it into a pr row for the DB."""
    return InboxPrRaw(
        pr_repo=repo,
        pr_number=number,
        title=title or f"PR #{number}",
        author_login="me",
        head_ref=head,
        base_ref=base,
        is_draft=False,
        url=f"https://github.com/{repo}/pull/{number}",
        updated_at="2026-05-14T00:00:00Z",
        ci_status="pass",
        sources=[source],
    )


def seed_inbox_row(
    db_path: Path,
    *,
    pr_repo: str = "o/r",
    pr_number: int = 1,
    title: str | None = None,
    author_login: str = "me",
    url: str | None = None,
    is_draft: bool = False,
    ci_status: str = "pass",
    sources: list[str] | None = None,
    notes: str | None = None,
    ticket: str | None = None,
    pr_updated_at: str = "2026-05-14T00:00:00Z",
    added_at: str = "2026-05-14T00:00:00Z",
    last_seen_at: str = "2026-05-14T00:00:00Z",
) -> PrRow:
    """Insert one inbox-flagged pr row + return it."""
    return seed_pr(
        db_path,
        pr_repo=pr_repo,
        pr_number=pr_number,
        is_inbox=True,
        inbox_added_at=added_at,
        inbox_sources=sources if sources is not None else ["reviewer"],
        title=title or f"PR #{pr_number}",
        author_login=author_login,
        url=url or f"https://github.com/{pr_repo}/pull/{pr_number}",
        is_draft=is_draft,
        ci_status=ci_status,
        notes=notes,
        ticket=ticket,
        pr_updated_at=pr_updated_at,
        last_seen_at=last_seen_at,
    )
