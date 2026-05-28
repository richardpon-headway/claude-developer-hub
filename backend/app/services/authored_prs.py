"""Request-time read for the user's authored PRs surface.

Reads directly from the unified ``pr`` table — discovery is
:mod:`app.services.authored_poll`'s job; this module only projects
PrRow rows that match the authored surface into the legacy
``AuthoredPrRow`` shape for the route handler.

Plan-61 will collapse this module entirely once the route handler
calls ``pr_db.list_pr_sync`` directly.
"""
from __future__ import annotations

import asyncio
import logging

from app.config.loader import load_config
from app.models.authored_pr import AuthoredPrRow
from app.services import authored_pr_notes_db, gh_identity, pr_db
from app.services.inbox_search import (
    configured_repos_index,
    extract_ticket,
    is_repo_configured,
)

log = logging.getLogger(__name__)


async def fetch_authored_prs() -> list[AuthoredPrRow]:
    """Return the user's open authored PRs from the unified pr table.

    The legacy implementation shelled ``gh search prs`` per request +
    deduped against worktree / inbox / bookmark. Plan-60 moved both
    duties: discovery is the authored_poll loop; dedup is the
    ``has_worktree=False, is_bookmarked=False, is_inbox=False``
    filter on this read.

    Falls back to an empty list when ``gh`` is unauthed (no local
    login resolved). Same fail-open contract as the legacy path.
    """
    local_login = await gh_identity.get_user_login()
    if local_login is None:
        return []

    config = load_config()
    repos_index = configured_repos_index(config.repos)

    rows = await asyncio.to_thread(
        pr_db.list_pr_sync,
        author_login=local_login,
        state="open",
        is_bookmarked=False,
        is_inbox=False,
        has_worktree=False,
        order_by="pr.pr_updated_at DESC",
    )

    out: list[AuthoredPrRow] = []
    for r in rows:
        out.append(
            AuthoredPrRow(
                pr_repo=r.pr_repo,
                pr_number=r.pr_number,
                title=r.title or "",
                url=r.url or "",
                is_draft=r.is_draft,
                ci_status=r.ci_status or "none",  # type: ignore[arg-type]
                ticket=r.ticket or extract_ticket(r.title or "", config.repos),
                pr_updated_at=r.pr_updated_at or "",
                repo_configured=is_repo_configured(r.pr_repo, repos_index),
                notes=r.notes,
            )
        )

    # The route handler historically attached notes via a batch call
    # to authored_pr_notes_db. Notes now live on pr.notes (set above
    # via r.notes), so the batch lookup is redundant — kept as a
    # belt-and-suspenders fallback for any row whose pr.notes is NULL
    # but an authored_pr_notes-style write happened through a
    # different path. Plan-61 removes this entirely.
    keys = {(r.pr_repo, r.pr_number) for r in out if r.notes is None}
    if keys:
        extra_notes = await asyncio.to_thread(
            authored_pr_notes_db.notes_by_keys_sync, keys
        )
        if extra_notes:
            for row in out:
                if row.notes is None:
                    note = extra_notes.get((row.pr_repo, row.pr_number))
                    if note is not None:
                        row.notes = note

    return out


async def fetch_authored_prs_safe() -> list[AuthoredPrRow]:
    """No-throw wrapper. The pr_db read path can't raise GhNotFound
    (the gh call moved to the discovery loop), so this just shields
    the caller from any unexpected runtime error."""
    try:
        return await fetch_authored_prs()
    except Exception as e:  # pragma: no cover — defensive
        log.warning("authored-prs read failed: %s; returning empty", e)
        return []
