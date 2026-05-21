"""Background poller for the persistent inbox.

Replaces the previous ephemeral in-memory cache with a SQLite-backed
inbox table (see migrations/009_persistent_inbox_bookmarks.sql).

Each tick:

1. ``gh search prs`` for the three remaining auto-watch sources
   (``review-requested:@me``, ``assignee:@me``, ``mentions:@me``).
   ``team-review-requested:<owner>/<slug>`` was dropped — see plan-48.
2. For each result, skip if it matches a tracked worktree or is in
   ``inbox_archived``. Otherwise upsert into the ``inbox`` table.
3. Auto-removal sweep: for inbox rows not seen this tick, probe a
   bounded number via ``gh pr view --json state`` and delete any
   whose state is no longer ``open``. Rows that are still open get a
   ``last_seen_at`` bump so they aren't re-probed every tick.

Failures inside the tick log and return — the persistent rows
already in SQLite stay intact, and the next tick retries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from app.config.loader import load_config
from app.config.schema import RepoConfig
from app.db import get_db_path, open_db
from app.models.inbox import InboxRow
from app.models.worktree import now_iso
from app.services import inbox_db
from app.services.gh_cli import GhNotFound, run_gh_json
from app.services.inbox_search import InboxPrRaw, fetch_inbox_prs

log = logging.getLogger(__name__)

_PR_URL_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")

# Stale-row probe budget per tick. At ~300ms per `gh pr view`, 10 calls
# ≈ 3s — fine for a 60s tick. Caps the blast radius if the inbox holds
# many long-untouched rows.
_AUTO_REMOVAL_PROBE_LIMIT = 10


def _tracked_pr_keys_sync() -> set[tuple[str, int]]:
    """Build the worktree-dedup set from BOTH source-of-truth columns:

    - ``worktree.pr_repo`` + ``worktree.pr_number`` (lazy-populated on
      pull-down / first PR-button click).
    - ``pr_state.payload`` parsed for the PR's URL (polled).

    The two sources can disagree on which workspaces have a known PR
    (one is lazy, the other is polled). Accepting matches from either
    means a PR that's been pulled down — by whichever path — doesn't
    re-appear in the inbox.
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
        for (payload_json,) in conn.execute(
            "SELECT payload FROM pr_state WHERE payload IS NOT NULL"
        ):
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            url = payload.get("url")
            n = payload.get("pr_number")
            if not isinstance(url, str) or not isinstance(n, int):
                continue
            m = _PR_URL_RE.match(url)
            if m is None:
                continue
            owner, name = m.group(1), m.group(2)
            keys.add((f"{owner}/{name}", n))
    finally:
        conn.close()
    return keys


def _extract_ticket(title: str, repos: list[RepoConfig]) -> str | None:
    """Try each configured repo's ``ticket_pattern`` against the PR
    title. Returns the first match, ``None`` if nothing matches.

    The inbox can hold PRs from unconfigured upstream repos, so we
    can't scope by ``pr_repo`` — we try every configured repo's
    pattern. Acceptable because patterns are user-specific anti-
    collision regexes (e.g. ``r"[A-Z]+-\\d+"``) and even a stray
    match produces a usable Jira link.
    """
    for repo in repos:
        if not repo.ticket_pattern:
            continue
        m = re.search(repo.ticket_pattern, title)
        if m:
            return m.group(0)
    return None


def _row_from_raw(
    raw: InboxPrRaw, *, now: str, repos: list[RepoConfig]
) -> InboxRow:
    return InboxRow(
        pr_repo=raw.pr_repo,
        pr_number=raw.pr_number,
        title=raw.title,
        author_login=raw.author_login,
        url=raw.url,
        is_draft=raw.is_draft,
        ci_status=raw.ci_status,
        sources=list(raw.sources),
        notes=None,
        ticket=_extract_ticket(raw.title, repos),
        pr_updated_at=raw.updated_at,
        added_at=now,
        last_seen_at=now,
    )


async def inbox_poll_loop(state) -> None:  # type: ignore[no-untyped-def]
    """Long-lived asyncio task. Re-reads polling interval each cycle so
    a config edit takes effect on the next tick without a daemon
    restart. Tick failures log and the loop continues.

    ``state`` is the FastAPI app state — preserved as an argument for
    the lifespan hook signature, but no longer used since the inbox
    now lives in SQLite, not in-process.
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


async def _tick(state) -> None:  # type: ignore[no-untyped-def]
    """One poll cycle: refresh from gh search, then probe stale rows."""
    config = load_config()

    try:
        raw = await fetch_inbox_prs()
    except GhNotFound:
        log.info("gh CLI not on PATH; inbox poll skipped this tick")
        return

    tracked = await asyncio.to_thread(_tracked_pr_keys_sync)
    archived = await asyncio.to_thread(inbox_db.archived_keys_sync)
    now = now_iso()

    upserts = 0
    for r in raw:
        key = (r.pr_repo, r.pr_number)
        if key in tracked or key in archived:
            continue
        row = _row_from_raw(r, now=now, repos=config.repos)
        await asyncio.to_thread(inbox_db.upsert_inbox_sync, row)
        upserts += 1

    # Auto-removal sweep. Rows whose ``last_seen_at < now`` were not
    # in this tick's gh search results — they may have closed / merged
    # (or fallen out of the user's review-requested list while
    # remaining open, which is the sticky-inbox case we explicitly
    # want to preserve).
    removed = await _auto_remove_closed(now)

    log.debug(
        "inbox tick: %d upserts, %d auto-removed; %d gh search hits",
        upserts, removed, len(raw),
    )


async def _auto_remove_closed(now: str) -> int:
    """Probe up to _AUTO_REMOVAL_PROBE_LIMIT stale rows via
    ``gh pr view --json state``. Delete any whose state is no longer
    ``open``; touch ``last_seen_at`` on the rest so they aren't
    re-probed every tick. Returns count of removed rows."""
    stale = await asyncio.to_thread(
        inbox_db.list_stale_inbox_sync, now, _AUTO_REMOVAL_PROBE_LIMIT
    )
    if not stale:
        return 0

    removed = 0
    for pr_repo, pr_number in stale:
        pr_state = await _gh_pr_state(pr_repo, pr_number)
        if pr_state is None:
            # gh failure or unexpected payload — leave the row alone
            # and let the next tick retry. Don't touch last_seen_at
            # so the row remains in the probe queue.
            continue
        if pr_state == "open":
            await asyncio.to_thread(_touch_last_seen, pr_repo, pr_number, now)
        else:
            await asyncio.to_thread(
                inbox_db.delete_inbox_sync, pr_repo, pr_number
            )
            removed += 1
    return removed


async def _gh_pr_state(pr_repo: str, pr_number: int) -> str | None:
    """Return ``'open'`` / ``'closed'`` / ``'merged'`` for a PR, or
    ``None`` if ``gh`` failed or returned an unexpected payload (so
    callers no-op instead of mistakenly removing rows)."""
    try:
        data = await run_gh_json(
            [
                "pr", "view", str(pr_number),
                "--repo", pr_repo,
                "--json", "state",
            ],
            swallow_errors=True,
        )
    except GhNotFound:
        return None
    if not isinstance(data, dict):
        return None
    s = data.get("state")
    if not isinstance(s, str):
        return None
    s_lower = s.lower()
    if s_lower in ("open", "closed", "merged"):
        return s_lower
    return None


def _touch_last_seen(pr_repo: str, pr_number: int, ts: str) -> None:
    """Bump ``last_seen_at`` on a row without changing other fields.
    Used by the auto-removal sweep when a probed row turns out to
    still be open."""
    db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE inbox SET last_seen_at = ? "
            "WHERE pr_repo = ? AND pr_number = ?",
            (ts, pr_repo, pr_number),
        )
        conn.commit()
    finally:
        conn.close()
