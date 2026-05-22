"""Fetch the user's authored PRs without persisting them.

Slice C of plan-48 introduces a "My PRs (no worktree)" tier in the
hub's Workspaces section. The rows come straight from ``gh search prs
--author:@me --state open``; nothing lives in SQLite. Filters out
PRs already covered by another surface (worktree, inbox row,
bookmark) so the same PR doesn't render twice.
"""
from __future__ import annotations

import asyncio
import logging

from app.config.loader import load_config
from app.models.authored_pr import AuthoredPrRow
from app.services import authored_pr_notes_db, bookmark_db, inbox_db
from app.services.gh_cli import GhNotFound, run_gh_json
from app.services.inbox_poll import _extract_ticket, _tracked_pr_keys_sync
from app.services.inbox_search import (
    _row_from_gh,
    configured_repos_index,
    is_repo_configured,
)

log = logging.getLogger(__name__)

_GH_SEARCH_JSON_FIELDS = (
    "number,title,url,isDraft,updatedAt,createdAt,author,repository,state"
)


async def fetch_authored_prs() -> list[AuthoredPrRow]:
    """Run ``gh search prs --author:@me --state open`` and dedup
    against every other surface that already shows this PR.

    Dedup sources:

    - Local worktrees (``worktree.pr_number`` + ``pr_state.payload``):
      pull-down already created a workspace.
    - Inbox (``inbox.pr_repo, pr_number``): the PR is already in the
      inbox surface — show there instead.
    - Bookmarks: explicit pin wins (consistent with the inbox dedup).

    Raises :class:`app.services.gh_cli.GhNotFound` if ``gh`` is missing.
    """
    data = await run_gh_json(
        [
            "search", "prs",
            "--author=@me",
            "--state=open",
            "--limit=100",
            "--json", _GH_SEARCH_JSON_FIELDS,
        ],
        cwd=None,
        swallow_errors=True,
    )
    if data is None:
        return []
    if not isinstance(data, list):
        log.warning("gh search prs --author returned non-list payload")
        return []

    tracked, inbox_keys, bookmark_keys = await asyncio.gather(
        asyncio.to_thread(_tracked_pr_keys_sync),
        asyncio.to_thread(inbox_db.inbox_pr_keys_sync),
        asyncio.to_thread(bookmark_db.bookmark_pr_keys_sync),
    )
    excluded = tracked | inbox_keys | bookmark_keys

    config = load_config()
    repos_index = configured_repos_index(config.repos)

    out: list[AuthoredPrRow] = []
    surviving_keys: set[tuple[str, int]] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw = _row_from_gh(entry, source="author")
        if raw is None:
            continue
        key = (raw.pr_repo, raw.pr_number)
        if key in excluded:
            continue
        surviving_keys.add(key)
        out.append(
            AuthoredPrRow(
                pr_repo=raw.pr_repo,
                pr_number=raw.pr_number,
                title=raw.title,
                url=raw.url,
                is_draft=raw.is_draft,
                ci_status=raw.ci_status,
                ticket=_extract_ticket(raw.title, config.repos),
                pr_updated_at=raw.updated_at,
                repo_configured=is_repo_configured(raw.pr_repo, repos_index),
                notes=None,
            )
        )

    # Attach persisted notes in one batch query rather than N round-
    # trips. Authored rows that don't yet have a note simply keep
    # ``notes=None``.
    notes_map = await asyncio.to_thread(
        authored_pr_notes_db.notes_by_keys_sync, surviving_keys
    )
    if notes_map:
        for row in out:
            note = notes_map.get((row.pr_repo, row.pr_number))
            if note is not None:
                row.notes = note

    # Newest-first, matching the inbox sort.
    out.sort(key=lambda r: r.pr_updated_at, reverse=True)
    return out


async def fetch_authored_prs_safe() -> list[AuthoredPrRow]:
    """Call :func:`fetch_authored_prs` and swallow ``gh`` missing — the
    route handler renders an empty list so the hub still loads even
    when ``gh`` isn't installed."""
    try:
        return await fetch_authored_prs()
    except GhNotFound:
        log.info("gh CLI not on PATH; authored-prs list empty this request")
        return []
