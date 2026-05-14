"""Cross-repo PR inbox: shell ``gh search prs`` for the user's open PRs
across all repos they have access to.

Three serial searches per poll, in priority order so the same PR can't
be attributed to a lower-priority source if a higher one already
matched:

1. ``author:@me`` → ``source="author"``
2. ``review-requested:@me`` → ``source="reviewer"``
3. For each configured team, ``team-review-requested:<owner>/<slug>`` →
   ``source="team:<owner>/<slug>"``

``gh search prs`` is a single network round-trip per call and runs
without a ``cwd`` (it queries github.com directly). Three sequential
calls at 60s cadence is well under any reasonable rate ceiling.

The service returns rows with raw fields + a coarse ``ci_status``
classifier; stack detection happens in :mod:`app.services.inbox_stack`
so the search code stays a thin adapter around ``gh``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.gh_cli import GhNotFound, run_gh_json

log = logging.getLogger(__name__)


# Fields requested from ``gh search prs --json``. statusCheckRollup is
# documented for ``gh pr view`` and works on ``gh search prs`` as well —
# the slice 1 spike confirmed it returns the simplified rollup shape
# (a list of {state, conclusion, status} entries, not the per-check
# breakdown). Enough to compute a coarse pass/fail/pending headline.
_GH_SEARCH_JSON_FIELDS = (
    "number,title,url,isDraft,updatedAt,createdAt,"
    "author,headRefName,baseRefName,headRepository,repository,"
    "statusCheckRollup"
)


@dataclass
class InboxPrRaw:
    """One PR row as it comes back from the inbox search, before stack
    annotation or repo-configured matching."""

    pr_repo: str  # "owner/name"
    pr_number: int
    title: str
    author_login: str
    head_ref: str
    base_ref: str
    is_draft: bool
    url: str
    updated_at: str
    ci_status: str  # "pass" | "fail" | "pending" | "none"
    source: str  # "author" | "reviewer" | "team:<slug>"


def _ci_status_from_rollup(rollup: list | None) -> str:
    """Reduce ``statusCheckRollup`` to a single label: fail beats
    pending beats pass beats none."""
    if not rollup:
        return "none"
    has_fail = False
    has_pending = False
    has_pass = False
    fail_states = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR"}
    pending_states = {"QUEUED", "IN_PROGRESS", "PENDING"}
    for c in rollup:
        # ``gh search prs`` rollup entries use uppercase ``state`` plus
        # sometimes a ``conclusion``. Be defensive — schema has changed
        # across gh versions.
        conclusion = (c.get("conclusion") or "").upper()
        state = (c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if conclusion == "SUCCESS" or state == "SUCCESS":
            has_pass = True
        elif conclusion in fail_states or state in fail_states:
            has_fail = True
        elif status in pending_states or state in pending_states:
            has_pending = True
    if has_fail:
        return "fail"
    if has_pending:
        return "pending"
    if has_pass:
        return "pass"
    return "none"


def _row_from_gh(entry: dict, *, source: str) -> InboxPrRaw | None:
    """Map one ``gh search prs`` JSON entry to an :class:`InboxPrRaw`.

    Returns ``None`` if a required field is missing — defensive against
    schema drift across ``gh`` versions; the alternative is the whole
    poll crashing on one malformed row.
    """
    number = entry.get("number")
    title = entry.get("title")
    url = entry.get("url")
    repo = entry.get("repository") or {}
    repo_owner = (repo.get("owner") or {}).get("login") or repo.get("nameWithOwner", "").split("/")[0]
    repo_name = repo.get("name")
    head_ref = entry.get("headRefName")
    base_ref = entry.get("baseRefName")
    author = (entry.get("author") or {}).get("login")
    updated = entry.get("updatedAt") or ""

    if not isinstance(number, int) or not title or not url or not repo_name or not repo_owner:
        log.info("skipping malformed gh search prs row: %s", entry)
        return None
    if not isinstance(head_ref, str) or not isinstance(base_ref, str):
        log.info("skipping gh search prs row without head/base ref: #%s", number)
        return None

    return InboxPrRaw(
        pr_repo=f"{repo_owner}/{repo_name}",
        pr_number=number,
        title=title,
        author_login=author or "",
        head_ref=head_ref,
        base_ref=base_ref,
        is_draft=bool(entry.get("isDraft")),
        url=url,
        updated_at=updated,
        ci_status=_ci_status_from_rollup(entry.get("statusCheckRollup")),
        source=source,
    )


async def _search(query: str, *, source: str) -> list[InboxPrRaw]:
    """Run one ``gh search prs`` invocation and parse the JSON array."""
    data = await run_gh_json(
        [
            "search",
            "prs",
            "--state=open",
            "--limit=100",
            "--json",
            _GH_SEARCH_JSON_FIELDS,
            query,
        ],
        cwd=None,
        swallow_errors=True,
    )
    if data is None:
        return []
    if not isinstance(data, list):
        log.warning("gh search prs returned non-list payload; skipping")
        return []
    out: list[InboxPrRaw] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        row = _row_from_gh(entry, source=source)
        if row is not None:
            out.append(row)
    return out


async def fetch_inbox_prs(teams: list[str]) -> list[InboxPrRaw]:
    """Run the three (or 2 + N-teams) search queries and dedupe with
    author > reviewer > team priority.

    Raises :class:`app.services.gh_cli.GhNotFound` if ``gh`` is missing —
    callers (the polling loop) catch and log once.
    """
    seen: dict[tuple[str, int], InboxPrRaw] = {}

    def _absorb(rows: list[InboxPrRaw]) -> None:
        for r in rows:
            key = (r.pr_repo, r.pr_number)
            # First write wins (priority order is the call order).
            seen.setdefault(key, r)

    try:
        _absorb(await _search("author:@me", source="author"))
        _absorb(await _search("review-requested:@me", source="reviewer"))
        for team in teams:
            _absorb(
                await _search(f"team-review-requested:{team}", source=f"team:{team}")
            )
    except GhNotFound:
        # Re-raise so the polling loop can log once-per-tick.
        raise

    return list(seen.values())


def filter_out_worktree_prs(
    prs: list[InboxPrRaw], tracked: set[tuple[str, int]]
) -> list[InboxPrRaw]:
    """Drop any inbox PR whose ``(pr_repo, pr_number)`` matches a
    locally-tracked worktree. ``tracked`` is built by the polling loop
    from a union of the ``worktree`` and ``pr_state`` tables (the two
    sources can disagree; we accept matches from either)."""
    return [p for p in prs if (p.pr_repo, p.pr_number) not in tracked]


def configured_repos_index(
    repos: list[Any],
) -> dict[str, Any]:
    """Index ``config.repos[]`` by the GitHub ``owner/name`` we expect
    a PR's ``pr_repo`` to match.

    Slice 1 uses a coarse heuristic: ``RepoConfig.name == <repo basename>``
    means a PR in ``<any-owner>/<basename>`` is considered "configured".
    Slice 2 makes this explicit via a new ``RepoConfig.github_repo``
    field; that will replace this heuristic. Returns a dict keyed by
    basename → RepoConfig so a caller can do ``index.get(basename)``.
    """
    return {repo.name: repo for repo in repos}


def is_repo_configured(pr_repo: str, repos_by_basename: dict[str, Any]) -> bool:
    """Slice-1 heuristic. ``pr_repo`` is ``owner/name``; we match on
    the ``name`` half against ``RepoConfig.name``."""
    parts = pr_repo.split("/", 1)
    if len(parts) != 2:
        return False
    return parts[1] in repos_by_basename
