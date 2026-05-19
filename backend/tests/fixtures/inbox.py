"""Inbox builders for tests.

The three helpers below construct the data shapes the inbox slice
operates on. They use sensible defaults — pass kwargs to override just
the fields a specific test cares about.
"""
from __future__ import annotations

from app.services.inbox_poll import InboxCache, InboxPr
from app.services.inbox_search import InboxPrRaw


def build_raw_pr(
    *,
    repo: str = "o/r",
    number: int = 1,
    head: str = "feat/x",
    base: str = "main",
    source: str = "author",
    title: str | None = None,
) -> InboxPrRaw:
    """Build an ``InboxPrRaw`` — the search-layer shape before stack
    annotation + configured-repo flagging."""
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


def build_enriched_pr(
    *,
    pr_repo: str = "o/r",
    pr_number: int = 1,
    repo_configured: bool = True,
    head_ref: str = "feat/x",
    author_login: str = "me",
) -> InboxPr:
    """Build a fully-enriched ``InboxPr`` (post stack annotation +
    repo-configured flagging) suitable for seeding the cache."""
    return InboxPr(
        pr_repo=pr_repo,
        pr_number=pr_number,
        title=f"PR #{pr_number}",
        author_login=author_login,
        head_ref=head_ref,
        base_ref="main",
        is_draft=False,
        url=f"https://github.com/{pr_repo}/pull/{pr_number}",
        updated_at="2026-05-14T00:00:00Z",
        ci_status="pass",
        sources=["author"],
        stack_top_pr_number=None,
        stack_size=1,
        stack_position=1,
        repo_configured=repo_configured,
    )


def seed_inbox_cache(*prs: InboxPr) -> InboxCache:
    """Wrap the given enriched PRs in an ``InboxCache`` with a fixed
    timestamp."""
    return InboxCache(prs=list(prs), checked_at="2026-05-14T00:00:00Z")
