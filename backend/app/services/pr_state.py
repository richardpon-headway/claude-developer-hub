"""PR state classification + persistence.

Wraps a single ``gh pr view --json …`` call into a typed summary that
maps to the same priority headlines the ``pr-check-action-required``
skill produces. Cached in the ``pr_state`` SQLite table so the hub's
workspace-list query can join it in and surface an ambient badge per
row.

Headline priority (first match wins, matching the skill):

0. ``merged`` / ``closed`` — terminal state from gh's ``state`` field
   wins over everything; once a PR is done, mid-flow signals like
   "ci_failing" or "human_comment" stop being meaningful.
1. ``ci_failing``        — any check has bucket=='fail'
2. ``merge_conflicts``   — mergeStateStatus=='DIRTY' or mergeable=='CONFLICTING'
3. ``in_merge_queue``    — currently we lack the merge-queue probe; reserved.
4. ``ready_to_merge``    — approved AND no failing/pending checks
5. ``human_comment``     — has any non-bot comment, not approved, not failing
                           (approximation; the skill uses timeline events to
                           compare "comment timestamp vs user's last push" —
                           too heavy for an ambient signal. False positives
                           are tolerable for v1, revisit if noisy.)
6. ``review_requested``  — review requests pending against the current user
                           (not classifiable from ``gh pr view`` alone; we
                           treat this the same as ``human_comment`` for now)
7. ``checks_running``    — any pending check, no fails, no other action
8. ``draft``             — isDraft and none of the above
9. ``waiting_on_others`` — default fallback when a PR exists
10. ``no_pr``            — gh reported no PR for the branch
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.db import get_db_path, open_db
from app.services.gh_cli import GhNotFound, run_gh_json

log = logging.getLogger(__name__)

BOT_LOGIN_PATTERN = re.compile(
    r"(bot|robot|actions|datadog|codecov|cursor|copilot|dependabot|renovate|semgrep)",
    re.IGNORECASE,
)

# Fields requested from `gh pr view --json` for one PR. Keep this list
# narrow — we pay for every field crossed over the GitHub API.
_GH_JSON_FIELDS = (
    "number,url,title,state,isDraft,mergeable,mergeStateStatus,"
    "reviewDecision,statusCheckRollup,comments,"
    "baseRefName,headRefName,updatedAt"
)


@dataclass
class PrChecks:
    # `passed` (not `pass`) avoids the Python keyword and reuses the
    # same word on the wire so the frontend type stays clean.
    passed: int = 0
    fail: int = 0
    pending: int = 0
    total: int = 0

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "fail": self.fail,
            "pending": self.pending,
            "total": self.total,
        }


@dataclass
class PrComments:
    human: int = 0
    bot: int = 0
    total: int = 0

    def as_dict(self) -> dict:
        return {"human": self.human, "bot": self.bot, "total": self.total}


@dataclass
class PrSummary:
    """Classified, serialized view of one PR's state. The shape here
    mirrors what the hub frontend reads from /api/worktrees and the
    refresh endpoint.

    ``labels`` lists every applicable signal in priority order;
    ``headline`` is kept for back-compat and equals ``labels[0]``.
    """

    headline: str
    pr_number: int | None = None
    url: str | None = None
    title: str | None = None
    is_draft: bool = False
    mergeable: str | None = None
    merge_state_status: str | None = None
    review_decision: str | None = None
    checks: PrChecks = field(default_factory=PrChecks)
    comments: PrComments = field(default_factory=PrComments)
    base_ref: str | None = None
    head_ref: str | None = None
    updated_at: str | None = None
    labels: list[str] = field(default_factory=list)
    # Number of PR review threads that are NOT resolved AND NOT
    # outdated (outdated = superseded by a force-push). Surfaces as
    # the ``unresolved_comments`` label when > 0.
    unresolved_threads: int = 0

    def to_payload(self) -> dict:
        d = asdict(self)
        d["checks"] = self.checks.as_dict()
        d["comments"] = self.comments.as_dict()
        return d


@dataclass
class PrStateRow:
    """A pr_state row as read back from SQLite, including the join-side
    metadata (checked_at). Payload is parsed back into the PrSummary
    fields plus the timestamp."""

    headline: str
    payload: dict
    checked_at: str


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------


# Priority order for labels. The hub's tier grouping uses ``labels[0]``
# to decide which tier a workspace sorts into AND as the back-compat
# ``headline``, so the order here matters and intentionally mirrors
# the original first-match-wins classifier:
#
# - Terminal states (``merged``/``closed``) lead because once a PR is
#   done, mid-flow signals like ``ci_failing`` stop being actionable —
#   the cleanup IS the action.
# - Within actionable signals, the loudest ones (CI fail, conflict)
#   lead the calmer ones (review, checks running).
_LABEL_PRIORITY: tuple[str, ...] = (
    "merged",
    "closed",
    "ci_failing",
    "merge_conflicts",
    # Unresolved review threads sit above generic ``human_comment`` —
    # the count is from GitHub's per-thread isResolved flag, so it's
    # a strictly more specific signal of "feedback needs addressing".
    "unresolved_comments",
    "human_comment",
    "review_requested",
    "ready_to_merge",
    "in_merge_queue",
    "checks_running",
    "waiting_on_others",
    "draft",
    "no_pr",
)


def _compute_labels(
    *,
    state: str | None,
    is_draft: bool,
    mergeable: str | None,
    merge_state_status: str | None,
    review_decision: str | None,
    checks: PrChecks,
    comments: PrComments,
    unresolved_threads: int = 0,
) -> list[str]:
    """Emit every label that applies, ordered by priority.

    Unlike the prior single-headline classifier, signals don't suppress
    each other — a PR can carry both ``ci_failing`` and ``human_comment``
    simultaneously, which is the case the labels-UI was designed for.
    Some labels are still mutually exclusive by their definition
    (``ready_to_merge`` requires zero fails/pending, so it can't
    co-occur with ``ci_failing`` or ``checks_running``).
    """
    found: set[str] = set()

    state_upper = (state or "").upper()
    if state_upper == "MERGED":
        found.add("merged")
    if state_upper == "CLOSED":
        found.add("closed")
    if checks.fail > 0:
        found.add("ci_failing")
    if (merge_state_status or "").upper() == "DIRTY" or (
        mergeable or ""
    ).upper() == "CONFLICTING":
        found.add("merge_conflicts")
    approved = (review_decision or "").upper() == "APPROVED"
    # `ready_to_merge`'s definition genuinely requires no fails/pending —
    # those aren't priority suppression, they're part of "ready".
    if approved and checks.fail == 0 and checks.pending == 0:
        found.add("ready_to_merge")
    # Below this point we deliberately drop the cross-signal guards
    # the old single-headline classifier carried (e.g. "no human_comment
    # if ci_failing"). Under multi-label semantics each signal stands
    # on its own; the UI surfaces them all as chips.
    if unresolved_threads > 0:
        found.add("unresolved_comments")
    if comments.human > 0 and not approved:
        found.add("human_comment")
    if checks.pending > 0:
        found.add("checks_running")
    if is_draft:
        found.add("draft")

    if not found:
        found.add("waiting_on_others")

    # Project onto the priority tuple to fix ordering and guarantee
    # labels[0] is the most-important signal.
    return [label for label in _LABEL_PRIORITY if label in found]


def _classify(
    *,
    state: str | None,
    is_draft: bool,
    mergeable: str | None,
    merge_state_status: str | None,
    review_decision: str | None,
    checks: PrChecks,
    comments: PrComments,
) -> str:
    """Back-compat shim: return the highest-priority label as a single
    string. New code should use :func:`_compute_labels` and read
    ``labels[0]`` directly."""
    labels = _compute_labels(
        state=state,
        is_draft=is_draft,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        checks=checks,
        comments=comments,
    )
    return labels[0]


def _count_checks(roll: list) -> PrChecks:
    """statusCheckRollup is a list of {name, status, conclusion} (for
    GitHub Actions) or {name, state} (for legacy commit statuses) plus
    a ``bucket`` field that newer gh versions add. We try ``bucket``
    first, then fall back to ``conclusion`` + ``status`` + ``state``."""
    out = PrChecks()
    for c in roll or []:
        bucket = (c.get("bucket") or "").lower()
        if not bucket:
            # Synthesize from the fields gh actually surfaces.
            conclusion = (c.get("conclusion") or "").lower()
            status = (c.get("status") or "").lower()
            state = (c.get("state") or "").lower()
            fail_conclusions = {"failure", "timed_out", "cancelled", "action_required"}
            pending_statuses = {"queued", "in_progress", "pending"}
            if conclusion == "success" or state == "success":
                bucket = "pass"
            elif conclusion in fail_conclusions or state in {"failure", "error"}:
                bucket = "fail"
            elif status in pending_statuses or state == "pending":
                bucket = "pending"
        out.total += 1
        if bucket == "pass":
            out.passed += 1
        elif bucket == "fail":
            out.fail += 1
        elif bucket == "pending":
            out.pending += 1
    return out


def _count_comments(comments: list) -> PrComments:
    out = PrComments()
    for c in comments or []:
        author = (c.get("author") or {}).get("login") or ""
        if BOT_LOGIN_PATTERN.search(author):
            out.bot += 1
        else:
            out.human += 1
    out.total = out.human + out.bot
    return out


def summarize_gh_payload(
    payload: dict | None, *, unresolved_threads: int = 0
) -> PrSummary:
    """Map a parsed ``gh pr view`` JSON dict into a PrSummary. ``None``
    or empty dict signals "no PR found for this branch".

    ``unresolved_threads`` is the count of un-resolved + un-outdated
    review threads, fetched separately via GraphQL by the caller; it
    flows through ``_compute_labels`` to emit the
    ``unresolved_comments`` label.
    """
    if not payload:
        return PrSummary(headline="no_pr", labels=["no_pr"])

    is_draft = bool(payload.get("isDraft"))
    mergeable = payload.get("mergeable")
    merge_state_status = payload.get("mergeStateStatus")
    review_decision = payload.get("reviewDecision")
    checks = _count_checks(payload.get("statusCheckRollup") or [])
    comments = _count_comments(payload.get("comments") or [])

    labels = _compute_labels(
        state=payload.get("state"),
        is_draft=is_draft,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        checks=checks,
        comments=comments,
        unresolved_threads=unresolved_threads,
    )

    return PrSummary(
        headline=labels[0],
        labels=labels,
        pr_number=payload.get("number"),
        url=payload.get("url"),
        title=payload.get("title"),
        is_draft=is_draft,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        checks=checks,
        comments=comments,
        base_ref=payload.get("baseRefName"),
        head_ref=payload.get("headRefName"),
        updated_at=payload.get("updatedAt"),
        unresolved_threads=unresolved_threads,
    )


# ---------------------------------------------------------------------
# Fetch — shells `gh pr view` inside the worktree path
# ---------------------------------------------------------------------


_PR_URL_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


_UNRESOLVED_THREADS_QUERY = """\
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes { isResolved, isOutdated }
      }
    }
  }
}
"""


async def _fetch_unresolved_threads_count(
    owner: str, name: str, pr_number: int
) -> int:
    """GraphQL fetch for the PR's reviewThread isResolved flags.

    Returns the count of threads that are both unresolved AND not
    outdated. Outdated threads (those superseded by a force-push) are
    skipped because they're no longer actionable.

    Fail-open: returns 0 on any gh / parse failure so a transient
    GraphQL hiccup doesn't make a noisy PR look clean… wait, actually
    returns 0 = "looks clean", which IS a false-negative. The
    polling loop will retry on the next tick; we accept silent skip
    over crashing the whole fetch.
    """
    try:
        data = await run_gh_json(
            [
                "api",
                "graphql",
                "-f",
                f"query={_UNRESOLVED_THREADS_QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={pr_number}",
            ],
            swallow_errors=True,
        )
    except GhNotFound:
        return 0
    if not isinstance(data, dict):
        return 0
    nodes = (
        ((data.get("data") or {}).get("repository") or {})
        .get("pullRequest") or {}
    ).get("reviewThreads", {}).get("nodes") or []
    return sum(
        1
        for t in nodes
        if t.get("isResolved") is False and t.get("isOutdated") is False
    )


async def fetch_pr_summary(worktree_path: Path) -> PrSummary:
    """Run ``gh pr view --json …`` inside ``worktree_path``, then
    fetch unresolved review-thread counts via GraphQL, and return a
    classified summary. Returns ``PrSummary(headline='no_pr')`` if
    gh reports no PR for the current branch.

    Raises :class:`app.services.gh_cli.GhNotFound` if ``gh`` isn't on
    PATH; the polling loop catches that and quiets per-row warnings.
    """
    payload = await run_gh_json(
        ["pr", "view", "--json", _GH_JSON_FIELDS],
        cwd=worktree_path,
        swallow_errors=True,
    )
    if payload is None:
        return PrSummary(headline="no_pr")
    if not isinstance(payload, dict):
        return summarize_gh_payload(None)

    # Best-effort second fetch: review-thread resolution status lives
    # behind GraphQL (not exposed via ``gh pr view --json``). Failure
    # silently returns 0 — the rest of the summary still surfaces.
    unresolved = 0
    url = payload.get("url")
    if isinstance(url, str):
        m = _PR_URL_RE.match(url)
        if m is not None:
            owner, name, pr_number = m.group(1), m.group(2), int(m.group(3))
            unresolved = await _fetch_unresolved_threads_count(
                owner, name, pr_number
            )

    return summarize_gh_payload(payload, unresolved_threads=unresolved)


# ---------------------------------------------------------------------
# Persistence (sync; wrap callers with asyncio.to_thread)
# ---------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert_pr_state_sync(
    repo: str,
    worktree_name: str,
    summary: PrSummary,
    db_path: Path | None = None,
) -> str:
    """Insert or replace the pr_state row. Returns the ``checked_at``
    timestamp written so callers can echo it back."""
    if db_path is None:
        db_path = get_db_path()
    checked_at = _now_iso()
    payload_json = json.dumps(summary.to_payload(), separators=(",", ":"))
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO pr_state (repo, worktree_name, headline, payload, checked_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, worktree_name) DO UPDATE SET "
            "  headline = excluded.headline, "
            "  payload = excluded.payload, "
            "  checked_at = excluded.checked_at",
            (repo, worktree_name, summary.headline, payload_json, checked_at),
        )
        conn.commit()
    finally:
        conn.close()
    return checked_at


def get_pr_state_sync(
    repo: str, worktree_name: str, db_path: Path | None = None
) -> PrStateRow | None:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT headline, payload, checked_at FROM pr_state "
            "WHERE repo = ? AND worktree_name = ?",
            (repo, worktree_name),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return PrStateRow(
        headline=row[0],
        payload=json.loads(row[1]),
        checked_at=row[2],
    )
