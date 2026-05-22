"""Cross-repo PR inbox: shell ``gh search prs`` for the user's open PRs
across all repos they have access to.

Three serial searches per poll, in priority order so the primary source
chip (``sources[0]``) reflects the highest-priority signal:

1. ``user-review-requested:@me`` → ``source="reviewer"`` (direct
   reviewer only — the ``user-review-requested`` qualifier excludes
   team-mediated requests at the search layer; no post-filter needed).
2. ``assignee:@me`` → ``source="assignee"``
3. ``mentions:@me`` → ``source="mentions"``

``author:@me`` and ``team-review-requested:<team>`` were both dropped
in the persistent-inbox redesign — see plan-48. Author-routed PRs
surface in the Workspaces section (auto-promote tier); team-mediated
review requests are no longer auto-watched at all (bookmark them
manually if you want one in your inbox).

``gh search prs`` is a single network round-trip per call and runs
without a ``cwd`` (it queries github.com directly). The service
returns rows with raw fields + a coarse ``ci_status`` classifier.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.gh_cli import GhNotFound, run_gh_json

log = logging.getLogger(__name__)


# Fields ``gh search prs --json`` actually supports.
#
# Earlier we asked for ``headRefName``, ``baseRefName``, ``headRepository``,
# and ``statusCheckRollup`` — those are ``gh pr view`` fields, NOT search
# fields. ``gh`` rejected the whole call with "Unknown JSON field",
# ``run_gh_json(swallow_errors=True)`` returned None, and the inbox came
# back empty. The valid set is from ``gh search prs --json NOPE`` error
# output: assignees, author, authorAssociation, body, closedAt,
# commentsCount, createdAt, id, isDraft, isLocked, isPullRequest, labels,
# number, repository, state, title, updatedAt, url.
#
# Consequence: head_ref/base_ref/ci_status/headRepository aren't
# available at search time. Stack detection collapses (every PR is its
# own size-1 stack); ci_status defaults to ``none``. A future
# enhancement can fan out to ``gh pr view`` per PR to re-populate these.
_GH_SEARCH_JSON_FIELDS = (
    "number,title,url,isDraft,updatedAt,createdAt,author,repository,state"
)


@dataclass
class InboxPrRaw:
    """One PR row as it comes back from the inbox search, before stack
    annotation or repo-configured matching.

    ``sources`` accumulates *every* reason the PR is in the inbox —
    a PR matching both author and team queries carries both. The list
    is priority-ordered (``author > reviewer > team:*``) so
    ``sources[0]`` is the primary signal used for subsection placement.
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

    Each call seeds ``sources`` with a single entry; the merge logic in
    :func:`fetch_inbox_prs` accumulates additional sources when the
    same PR comes back from a later search query.

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

    return InboxPrRaw(
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


async def fetch_inbox_prs() -> list[InboxPrRaw]:
    """Run every per-source search query and merge results.

    Unlike a priority-dedup, this accumulates *every* source a PR
    matches — a PR returned by both ``user-review-requested:@me`` and
    ``mentions:@me`` carries both labels. The first row for a given
    ``(pr_repo, pr_number)`` is kept as the carrier of the immutable
    fields (title etc.); subsequent matches just append to its
    ``sources`` list.

    Call order defines the source priority (sources[0] = primary):
    ``reviewer`` → ``assignee`` → ``mentions``.

    The reviewer query uses ``user-review-requested:@me`` rather than
    ``review-requested:@me`` so GitHub itself excludes team-mediated
    requests at the search layer. The earlier per-row post-filter
    (one ``gh pr view --json reviewRequests`` per candidate) is gone;
    one search call replaces ~N PR-view calls per tick.

    Raises :class:`app.services.gh_cli.GhNotFound` if ``gh`` is missing —
    callers (the polling loop) catch and log once.
    """
    seen: dict[tuple[str, int], InboxPrRaw] = {}

    def _absorb(rows: list[InboxPrRaw]) -> None:
        for r in rows:
            key = (r.pr_repo, r.pr_number)
            existing = seen.get(key)
            if existing is None:
                seen[key] = r
                continue
            # Same PR seen earlier under a higher-priority query.
            # Append this row's sources to the prior one's, preserving
            # priority order + skipping duplicates.
            for s in r.sources:
                if s not in existing.sources:
                    existing.sources.append(s)

    try:
        _absorb(await _search("user-review-requested:@me", source="reviewer"))
        _absorb(await _search("assignee:@me", source="assignee"))
        _absorb(await _search("mentions:@me", source="mentions"))
    except GhNotFound:
        # Re-raise so the polling loop can log once-per-tick.
        raise

    return list(seen.values())


@dataclass
class ReposIndex:
    """Resolved lookup tables for matching an inbox PR's ``pr_repo`` to
    a configured :class:`RepoConfig`. Built once per poll tick.

    - ``by_github_repo``: explicit ``owner/name`` mapping for repos
      that declare ``github_repo``. Authoritative when present.
    - ``by_basename``: fallback mapping keyed on ``RepoConfig.name``
      (which usually equals the GitHub repo basename). Lets configs
      that haven't been retrofitted with ``github_repo`` still
      participate in inbox matching.
    """

    by_github_repo: dict[str, Any]
    by_basename: dict[str, Any]


def configured_repos_index(repos: list[Any]) -> ReposIndex:
    by_github_repo: dict[str, Any] = {}
    by_basename: dict[str, Any] = {}
    for repo in repos:
        if getattr(repo, "github_repo", None):
            by_github_repo[repo.github_repo] = repo
        by_basename[repo.name] = repo
    return ReposIndex(by_github_repo=by_github_repo, by_basename=by_basename)


def lookup_configured_repo(pr_repo: str, index: ReposIndex) -> Any | None:
    """Return the :class:`RepoConfig` matching ``pr_repo``, preferring
    the explicit ``github_repo`` mapping, falling back to a basename
    match against ``RepoConfig.name`` ONLY for repos that haven't
    declared ``github_repo``. Returns ``None`` if nothing matches.

    Excluding repos that have ``github_repo`` set from the basename
    fallback prevents a misleading "configured" match: if the user
    explicitly told CDH "this repo is ``corp/myapp``", a PR from
    ``acme/myapp`` shouldn't piggy-back on that config — the user
    opted into precision when they set the field.
    """
    if pr_repo in index.by_github_repo:
        return index.by_github_repo[pr_repo]
    parts = pr_repo.split("/", 1)
    if len(parts) != 2:
        return None
    candidate = index.by_basename.get(parts[1])
    if candidate is None or getattr(candidate, "github_repo", None) is not None:
        return None
    return candidate


def is_repo_configured(pr_repo: str, index: ReposIndex) -> bool:
    return lookup_configured_repo(pr_repo, index) is not None
