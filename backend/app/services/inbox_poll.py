"""Background poller for the cross-repo PR inbox.

Mirrors the shape of :mod:`app.services.pr_state_poll`: a periodic tick
that calls ``gh`` and updates an in-process cache. Differences:

- The inbox query is cross-repo (one ``gh search prs`` covers everything),
  so there's no per-row fan-out and no SQLite cache — results live on
  ``app.state.inbox`` and are recomputed every tick.
- Dedup against locally-tracked worktrees pulls from BOTH the
  ``worktree.pr_number`` columns and ``pr_state.payload.pr_number``,
  since those two sources can disagree (one is lazy-on-click, the other
  is polled).
- On any failure the prior cache is preserved so the UI doesn't blank
  out during transient ``gh`` hiccups.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from app.config.loader import load_config
from app.db import get_db_path, open_db
from app.models.worktree import now_iso
from app.services.gh_cli import GhNotFound
from app.services.inbox_search import (
    InboxPrRaw,
    configured_repos_index,
    fetch_inbox_prs,
    filter_out_worktree_prs,
    is_repo_configured,
)
from app.services.inbox_stack import annotate_stacks

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60.0


@dataclass
class InboxPr:
    """One enriched inbox row: raw fields from ``gh`` + stack
    annotation + repo-configured flag. This is what the API returns
    after the polling tick processes raw search rows."""

    pr_repo: str
    pr_number: int
    title: str
    author_login: str
    head_ref: str
    base_ref: str
    is_draft: bool
    url: str
    updated_at: str
    ci_status: str
    source: str
    stack_top_pr_number: int | None
    stack_size: int
    stack_position: int
    repo_configured: bool


@dataclass
class InboxCache:
    """Mutable in-process state owned by ``app.state.inbox``. The HTTP
    route reads from here on every request; the poll loop overwrites
    ``prs`` and ``checked_at`` each tick."""

    prs: list[InboxPr] = field(default_factory=list)
    checked_at: str | None = None


def _tracked_pr_keys_sync() -> set[tuple[str, int]]:
    """Build the dedup set from BOTH source-of-truth columns:

    - ``worktree.pr_repo`` + ``worktree.pr_number`` (lazy-populated on
      first PR-button click).
    - ``pr_state.payload`` parsed for ``pr_number`` (polled).

    The two sources can disagree on which workspaces have a known PR
    (one is lazy, the other is polled). Accepting matches from either
    means a PR that's been pulled down — by whichever path — disappears
    from the inbox.
    """
    db_path = get_db_path()
    conn = open_db(db_path)
    keys: set[tuple[str, int]] = set()
    try:
        for repo_, n in conn.execute(
            "SELECT pr_repo, pr_number FROM worktree "
            "WHERE pr_repo IS NOT NULL AND pr_number IS NOT NULL"
        ):
            if repo_ and isinstance(n, int):
                keys.add((repo_, n))
        # pr_state has the PR number inside the JSON payload. We also
        # need the (owner/name) — that's not stored directly anywhere on
        # pr_state, so we join through worktree to recover it. A PR with
        # a pr_state row but no pr_repo on its worktree slips through;
        # that's acceptable since pr_repo gets populated on first
        # button-click and the redundancy will heal on the next poll.
        for repo_, payload_json in conn.execute(
            "SELECT w.pr_repo, ps.payload FROM pr_state ps "
            "JOIN worktree w ON w.repo = ps.repo AND w.name = ps.worktree_name "
            "WHERE w.pr_repo IS NOT NULL"
        ):
            if not repo_:
                continue
            try:
                payload = json.loads(payload_json)
                n = payload.get("pr_number")
                if isinstance(n, int):
                    keys.add((repo_, n))
            except (TypeError, ValueError):
                continue
    finally:
        conn.close()
    return keys


def _enrich(raw: list[InboxPrRaw]) -> list[InboxPr]:
    """Apply stack annotation + repo-configured flag. The dedup step
    runs separately before this so we don't waste compute on rows that
    are about to be dropped."""
    stack_by_key = annotate_stacks(raw)
    config = load_config()
    repos_index = configured_repos_index(config.repos)

    out: list[InboxPr] = []
    for r in raw:
        ann = stack_by_key[(r.pr_repo, r.pr_number)]
        out.append(
            InboxPr(
                pr_repo=r.pr_repo,
                pr_number=r.pr_number,
                title=r.title,
                author_login=r.author_login,
                head_ref=r.head_ref,
                base_ref=r.base_ref,
                is_draft=r.is_draft,
                url=r.url,
                updated_at=r.updated_at,
                ci_status=r.ci_status,
                source=r.source,
                stack_top_pr_number=ann.stack_top_pr_number,
                stack_size=ann.stack_size,
                stack_position=ann.stack_position,
                repo_configured=is_repo_configured(r.pr_repo, repos_index),
            )
        )
    return out


async def inbox_poll_loop(state) -> None:  # type: ignore[no-untyped-def]
    """Long-lived asyncio task. Reads ``config.inbox.teams`` fresh each
    tick so a config edit propagates to the next poll without a daemon
    restart. Failures don't crash the loop — they log and the next
    tick retries."""
    # Ensure the state has an inbox cache attribute even before the
    # first successful tick, so the HTTP route can render "loading".
    if not hasattr(state, "inbox"):
        state.inbox = InboxCache()

    while True:
        try:
            await _tick(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "inbox poll tick failed: %s; preserving prior cache, will retry",
                e,
            )
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def _tick(state) -> None:  # type: ignore[no-untyped-def]
    config = load_config()
    teams = list(config.inbox.teams)

    try:
        raw = await fetch_inbox_prs(teams)
    except GhNotFound:
        log.info("gh CLI not on PATH; inbox poll skipped this tick")
        return

    tracked = await asyncio.to_thread(_tracked_pr_keys_sync)
    raw = filter_out_worktree_prs(raw, tracked)
    enriched = _enrich(raw)
    state.inbox = InboxCache(prs=enriched, checked_at=now_iso())
