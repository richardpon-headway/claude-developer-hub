"""Cross-repo PR search helpers — discovery for the authored poll.

Authored discovery (``fetch_authored_prs_raw``) runs one
``gh search prs --author:@me`` query and maps each result through the
shared row mapper.

``gh search prs`` is a single network round-trip per call and runs
without a ``cwd`` (it queries github.com directly). The helpers return
rows with raw fields + a coarse ``ci_status`` classifier.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.gh_cli import run_gh_json

if TYPE_CHECKING:
    from app.config.schema import RepoConfig

log = logging.getLogger(__name__)


def extract_ticket(title: str, repos: list[RepoConfig]) -> str | None:
    """Try each configured repo's ``ticket_pattern`` against a PR
    title. Returns the first match, ``None`` if nothing matches.

    Discovery loops can see PRs from unconfigured upstream repos, so
    we can't scope by ``pr_repo`` — we try every configured repo's
    pattern. Acceptable because patterns are user-specific anti-
    collision regexes (e.g. ``r"[A-Z]+-\\d+"``) and even a stray
    match produces a usable Jira link.
    """
    for repo in repos:
        pattern = getattr(repo, "ticket_pattern", None)
        if not pattern:
            continue
        m = re.search(pattern, title)
        if m:
            return m.group(0)
    return None


# Fields ``gh search prs --json`` actually supports.
#
# Earlier we asked for ``headRefName``, ``baseRefName``, ``headRepository``,
# and ``statusCheckRollup`` — those are ``gh pr view`` fields, NOT search
# fields. ``gh`` rejected the whole call with "Unknown JSON field" and
# ``run_gh_json(swallow_errors=True)`` returned None. The valid set is
# from ``gh search prs --json NOPE`` error output: assignees, author,
# authorAssociation, body, closedAt, commentsCount, createdAt, id,
# isDraft, isLocked, isPullRequest, labels, number, repository, state,
# title, updatedAt, url.
#
# Consequence: head_ref/base_ref/ci_status/headRepository aren't
# available at search time; ci_status defaults to ``none``. A future
# enhancement can fan out to ``gh pr view`` per PR to re-populate these.
_GH_SEARCH_JSON_FIELDS = (
    "number,title,url,isDraft,updatedAt,createdAt,author,repository,state"
)


@dataclass
class PrSearchRaw:
    """One PR row as it comes back from ``gh search prs``, before
    repo-configured matching.

    ``sources`` records why the PR surfaced (currently always
    ``["author"]``); kept as a list so the row flows through the same
    projection paths the multi-source discovery once used.
    """

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
    sources: list[str]


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
        # Rollup entries use uppercase ``state`` plus sometimes a
        # ``conclusion``. Be defensive — schema has changed across gh
        # versions.
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


def _row_from_gh(entry: dict, *, source: str) -> PrSearchRaw | None:
    """Map one ``gh search prs`` JSON entry to a :class:`PrSearchRaw`.

    Returns ``None`` if a required field is missing — defensive against
    schema drift across ``gh`` versions; the alternative is the whole
    poll crashing on one malformed row.

    ``head_ref``/``base_ref``/``ci_status`` are not available from
    ``gh search prs`` (those are ``gh pr view`` fields), so they're
    filled with empty/``none`` placeholders.
    """
    number = entry.get("number")
    title = entry.get("title")
    url = entry.get("url")
    repo = entry.get("repository") or {}
    # `gh search prs` returns the repository as
    # ``{name, nameWithOwner, isFork, isPrivate, ...}`` — no nested
    # ``owner`` object. Use nameWithOwner directly.
    name_with_owner = repo.get("nameWithOwner")
    repo_name = repo.get("name")
    author = (entry.get("author") or {}).get("login")
    updated = entry.get("updatedAt") or ""

    if (
        not isinstance(number, int)
        or not title
        or not url
        or not repo_name
        or not isinstance(name_with_owner, str)
        or "/" not in name_with_owner
    ):
        log.info("skipping malformed gh search prs row: %s", entry)
        return None

    return PrSearchRaw(
        pr_repo=name_with_owner,
        pr_number=number,
        title=title,
        author_login=author or "",
        head_ref="",
        base_ref="",
        is_draft=bool(entry.get("isDraft")),
        url=url,
        updated_at=updated,
        ci_status="none",
        sources=[source],
    )


async def _search(query: str, *, source: str) -> list[PrSearchRaw]:
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
    out: list[PrSearchRaw] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        row = _row_from_gh(entry, source=source)
        if row is not None:
            out.append(row)
    return out


async def fetch_authored_prs_raw() -> list[PrSearchRaw]:
    """Run ``gh search prs --author:@me --state=open`` and map results
    through the shared row mapper.

    Each row's ``sources`` is seeded to ``["author"]``. Raises
    :class:`app.services.gh_cli.GhNotFound` if ``gh`` is missing —
    callers (the polling loop) catch and log once-per-tick.
    """
    return await _search("author:@me", source="author")
