"""Background discovery loop for the user's own open PRs.

Each tick:

1. Shell ``gh search prs --author=@me --state=open`` via
   :func:`pr_search.fetch_authored_prs_raw` (single network round
   trip).
2. For each result, upsert into the unified ``pr`` table with the
   local user's gh login as ``author_login`` and ``last_seen_at`` set
   to the tick start. Authoring isn't an origin flag — the
   ``author_login`` column is what identifies these rows for the
   authored surface.
3. Sweep: any ``pr`` row whose ``author_login = local_login`` AND
   whose ``last_seen_at < tick_start`` AND that has no origin flag,
   no notes, and no worktree → call
   :func:`pr_db.maybe_gc_sync`. The ``last_seen_at`` window prevents
   spuriously GC'ing rows that fell out of the search limit on a
   single tick.
4. Terminal-state resolution: a stale row that survives GC (a note or
   worktree pins it) but is still cached as ``state == "open"`` has
   dropped out of the open-PR search, so its "open" is stale. Refresh
   it via :func:`pr_enrichment_poll.enrich_pr` so its real terminal
   state (merged/closed) replaces the stale "open" — otherwise a
   note-pinned merged PR stays stuck in My Work. Gated on
   ``state == "open"`` so it fires once per PR, not every tick.

Fail-open: a missing/unreachable ``gh`` returns early WITHOUT running
the GC step. GC fires only after a confirmed-successful search so a
gh outage doesn't wipe every authored row.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config.loader import load_config
from app.models.pr import PrRow
from app.models.worktree import now_iso
from app.services import gh_identity, pr_db, pr_enrichment_poll
from app.services.gh_cli import GhNotFound
from app.services.pr_search import (
    extract_ticket,
    fetch_authored_prs_raw,
)

log = logging.getLogger(__name__)


async def authored_poll_loop(state: Any) -> None:  # noqa: ARG001 — kept for lifespan symmetry
    """Long-lived asyncio task. Tick failures log + retry; cancellation
    propagates so the lifespan teardown is clean."""
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "authored poll tick failed: %s; will retry next interval", e
            )
        try:
            interval = load_config().polling.authored_interval_seconds
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


async def _tick() -> None:
    """One discovery cycle: search + upsert + GC."""
    local_login = await gh_identity.get_user_login()
    if local_login is None:
        log.info(
            "gh CLI not on PATH or unauthed; authored poll skipped this tick"
        )
        return

    try:
        raw_rows = await fetch_authored_prs_raw()
    except GhNotFound:
        log.info("gh CLI not on PATH; authored poll skipped this tick")
        return

    config = load_config()
    tick_started = now_iso()

    for raw in raw_rows:
        ticket = extract_ticket(raw.title, config.repos)
        await asyncio.to_thread(
            pr_db.upsert_pr_sync,
            PrRow(
                pr_repo=raw.pr_repo,
                pr_number=raw.pr_number,
                title=raw.title,
                # gh search returns each entry's actual author. We
                # could overwrite with local_login as a sanity check;
                # using the payload directly survives shared accounts
                # / org bots / login renames more gracefully.
                author_login=raw.author_login or local_login,
                url=raw.url,
                is_draft=raw.is_draft,
                ci_status=raw.ci_status,  # type: ignore[arg-type]
                ticket=ticket,
                pr_updated_at=raw.updated_at,
                last_seen_at=tick_started,
            ),
        )

    # GC any rows that this tick's search didn't return AND no other
    # surface holds. Identifying "previously authored, no longer
    # returned" via the last_seen_at watermark + author_login filter.
    stale = await asyncio.to_thread(
        _list_stale_authored_sync, local_login, tick_started
    )
    for pr_repo, pr_number, state in stale:
        deleted = await asyncio.to_thread(
            pr_db.maybe_gc_sync, pr_repo, pr_number
        )
        # Row was pinned by a note/worktree so GC couldn't reap it, yet
        # it's no longer in the open-PR search — its cached "open" is
        # stale. Resolve the real terminal state once so it stops
        # masquerading as an open authored PR in My Work.
        if not deleted and state == "open":
            await pr_enrichment_poll.enrich_pr(pr_repo, pr_number)


def _list_stale_authored_sync(
    local_login: str, cutoff: str
) -> list[tuple[str, int, str | None]]:
    """Pr rows whose ``author_login == local_login`` AND
    ``last_seen_at < cutoff`` AND no bookmark holds the row, as
    ``(pr_repo, pr_number, state)`` triples.

    The ``maybe_gc_sync`` call after returning these candidates is the
    actual delete; ``state`` lets the caller decide whether a surviving
    (pinned) row still needs its stale "open" resolved to a terminal
    state.
    """
    from app.db import get_db_path, open_db

    db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT pr_repo, pr_number, state FROM pr "
            "WHERE author_login = ? "
            "  AND last_seen_at IS NOT NULL "
            "  AND last_seen_at < ? "
            "  AND is_bookmarked = 0",
            (local_login, cutoff),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()
