"""Inbox builders for tests.

The helpers below construct the data shapes the persistent-inbox slice
operates on. They use sensible defaults — pass kwargs to override
just the fields a specific test cares about.

After plan-48 the in-memory ``InboxCache`` / ``InboxPr`` are gone;
seed test rows directly into the ``inbox`` SQLite table via
:func:`seed_inbox_row`.
"""
from __future__ import annotations

from pathlib import Path

from app.models.inbox import InboxRow
from app.services import inbox_db
from app.services.inbox_search import InboxPrRaw


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
    poll tick maps it into an ``InboxRow`` for the DB."""
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


def build_inbox_row(
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
) -> InboxRow:
    """Build a persisted-inbox row with sensible defaults."""
    return InboxRow(
        pr_repo=pr_repo,
        pr_number=pr_number,
        title=title or f"PR #{pr_number}",
        author_login=author_login,
        url=url or f"https://github.com/{pr_repo}/pull/{pr_number}",
        is_draft=is_draft,
        ci_status=ci_status,  # type: ignore[arg-type]
        sources=sources if sources is not None else ["reviewer"],
        notes=notes,
        ticket=ticket,
        pr_updated_at=pr_updated_at,
        added_at=added_at,
        last_seen_at=last_seen_at,
    )


def seed_inbox_row(db_path: Path, **kwargs: object) -> InboxRow:
    """Convenience: build an ``InboxRow`` + insert it via the upsert
    helper. Returns the row that was inserted so tests can chain on
    it (assert fields, archive it, etc.)."""
    row = build_inbox_row(**kwargs)  # type: ignore[arg-type]
    inbox_db.upsert_inbox_sync(row, db_path=db_path)
    return row
