"""Background discovery loop for inbox PRs.

Each tick:

1. Run :func:`inbox_search.fetch_inbox_prs` (three serial
   ``gh search prs`` queries — reviewer / assignee / mentions).
2. Upsert every result into the unified ``pr`` table via
   :func:`pr_db.upsert_pr_sync` with ``is_inbox=True`` and
   ``last_seen_at = tick_start``. The unified-row model allows the
   bookmark / worktree / inbox flags to coexist on one row; the
   inbox-shim's ``list_inbox_sync`` filters out bookmarked /
   worktree-attached rows so the legacy "bookmark wins over inbox"
   surface precedence is preserved without write-side dedup.
3. Auto-removal sweep: for inbox-flagged rows whose ``last_seen_at <
   tick_start``, check ``pr.state`` (filled by the enrichment loop);
   if state is ``closed`` or ``merged``, clear the inbox + archive
   flags and call :func:`pr_db.maybe_gc_sync`. The row evaporates if
   no other surface holds it, else it persists with the remaining
   flags intact.

On the first deploy or shortly after a cold start, ``pr.state`` may
not yet be set by the enrichment loop. The sweep tolerates one cycle
of "stale rows linger until enrichment fills state" — closed PRs
visible in the inbox until the first enrichment tick (≤
``pr_enrichment_interval_seconds`` later).
"""
from __future__ import annotations

import asyncio
import logging

from app.config.loader import load_config
from app.models.pr import PrRow
from app.models.worktree import now_iso
from app.services import pr_db
from app.services.gh_cli import GhNotFound
from app.services.inbox_search import (
    InboxPrRaw,
    extract_ticket,
    fetch_inbox_prs,
)

log = logging.getLogger(__name__)


async def inbox_poll_loop(state) -> None:  # type: ignore[no-untyped-def]
    """Long-lived asyncio task. Re-reads polling interval each cycle so
    a config edit takes effect on the next tick without a daemon
    restart. Tick failures log and the loop continues.

    ``state`` is the FastAPI app state — preserved as an argument for
    the lifespan hook signature, but no longer read since the inbox
    lives in SQLite, not in-process.
    """
    while True:
        try:
            await _tick(state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "inbox poll tick failed: %s; persistent rows preserved, will retry",
                e,
            )
        try:
            interval = load_config().polling.inbox_interval_seconds
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def _tick(state) -> None:  # type: ignore[no-untyped-def]  # noqa: ARG001 — kept for route + lifespan symmetry
    """One poll cycle: discover via gh search, then prune closed rows."""
    config = load_config()

    try:
        raw = await fetch_inbox_prs()
    except GhNotFound:
        log.info("gh CLI not on PATH; inbox poll skipped this tick")
        return

    tick_started = now_iso()

    for r in raw:
        await asyncio.to_thread(pr_db.upsert_pr_sync, _pr_row_from_raw(
            r, ticket=extract_ticket(r.title, config.repos), now=tick_started
        ))

    removed = await _auto_remove_closed(tick_started)

    log.debug(
        "inbox tick: %d upserts, %d auto-removed; %d gh search hits",
        len(raw), removed, len(raw),
    )


def _pr_row_from_raw(raw: InboxPrRaw, *, ticket: str | None, now: str) -> PrRow:
    """Map an inbox-search row to a ``PrRow`` upsert. ``inbox_added_at``
    only takes the current timestamp on first insert (COALESCE-on-
    upsert preserves the prior value on subsequent ticks)."""
    return PrRow(
        pr_repo=raw.pr_repo,
        pr_number=raw.pr_number,
        is_inbox=True,
        inbox_added_at=now,
        inbox_sources=list(raw.sources),
        title=raw.title,
        author_login=raw.author_login,
        url=raw.url,
        is_draft=raw.is_draft,
        ci_status=raw.ci_status,  # type: ignore[arg-type]
        ticket=ticket,
        pr_updated_at=raw.updated_at,
        last_seen_at=now,
    )


async def _auto_remove_closed(now: str) -> int:
    """For inbox-flagged rows whose ``last_seen_at < now`` (i.e., the
    current search didn't return them), check ``pr.state``. If state
    is ``closed`` or ``merged`` — populated by the enrichment loop —
    clear the inbox + archive flags and GC the row.

    Returns count of rows whose inbox flag was cleared.
    """
    candidates = await asyncio.to_thread(_list_stale_inbox_with_state_sync, now)
    if not candidates:
        return 0

    removed = 0
    for pr_repo, pr_number, state in candidates:
        if state not in ("closed", "merged"):
            continue
        await asyncio.to_thread(
            pr_db.set_inbox_flag_sync, pr_repo, pr_number, False
        )
        await asyncio.to_thread(
            pr_db.set_archived_flag_sync, pr_repo, pr_number, False
        )
        await asyncio.to_thread(
            pr_db.maybe_gc_sync, pr_repo, pr_number
        )
        removed += 1
    return removed


def _list_stale_inbox_with_state_sync(
    cutoff: str,
) -> list[tuple[str, int, str | None]]:
    """``[(pr_repo, pr_number, state)]`` for inbox-flagged rows with
    ``last_seen_at < cutoff``. State may be NULL if the enrichment
    loop hasn't visited yet — caller skips those for one cycle."""
    from app.db import get_db_path, open_db

    db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number, state FROM pr "
            "WHERE is_inbox = 1 AND last_seen_at < ?",
            (cutoff,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()
