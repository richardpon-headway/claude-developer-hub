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
from app.services.gh_cli import run_gh_json

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
    refresh endpoint."""

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
    """Apply the priority-order rules above to raw gh fields, return
    the headline."""
    # Terminal states win over everything — once a PR is merged or
    # closed, "CI failing" or "human comment" classifications stop being
    # meaningful, and the row should visibly indicate the PR is done.
    state_upper = (state or "").upper()
    if state_upper == "MERGED":
        return "merged"
    if state_upper == "CLOSED":
        return "closed"
    if checks.fail > 0:
        return "ci_failing"
    if (merge_state_status or "").upper() == "DIRTY" or (mergeable or "").upper() == "CONFLICTING":
        return "merge_conflicts"
    # in_merge_queue: skipped — no signal in `gh pr view` alone.
    approved = (review_decision or "").upper() == "APPROVED"
    if approved and checks.fail == 0 and checks.pending == 0:
        return "ready_to_merge"
    if comments.human > 0 and not approved and checks.fail == 0:
        return "human_comment"
    if checks.pending > 0 and checks.fail == 0:
        return "checks_running"
    if is_draft:
        return "draft"
    return "waiting_on_others"


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


def summarize_gh_payload(payload: dict | None) -> PrSummary:
    """Map a parsed ``gh pr view`` JSON dict into a PrSummary. ``None``
    or empty dict signals "no PR found for this branch"."""
    if not payload:
        return PrSummary(headline="no_pr")

    is_draft = bool(payload.get("isDraft"))
    mergeable = payload.get("mergeable")
    merge_state_status = payload.get("mergeStateStatus")
    review_decision = payload.get("reviewDecision")
    checks = _count_checks(payload.get("statusCheckRollup") or [])
    comments = _count_comments(payload.get("comments") or [])

    headline = _classify(
        state=payload.get("state"),
        is_draft=is_draft,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        checks=checks,
        comments=comments,
    )

    return PrSummary(
        headline=headline,
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
    )


# ---------------------------------------------------------------------
# Fetch — shells `gh pr view` inside the worktree path
# ---------------------------------------------------------------------


async def fetch_pr_summary(worktree_path: Path) -> PrSummary:
    """Run ``gh pr view --json …`` inside ``worktree_path`` and return
    a classified summary. Returns ``PrSummary(headline='no_pr')`` if
    gh reports no PR for the current branch — that's the normal
    "branch hasn't been pushed yet" case, not an error.

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
    # gh pr view returns a dict; the cast is for type-narrowing only.
    return summarize_gh_payload(payload if isinstance(payload, dict) else None)


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
