"""Pydantic shape for a worktree row.

Mirrors the ``worktree`` table in migrations/001_initial.sql. Used by the
REST layer to serialize rows and by the service layer to type its returns.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorktreeStatus = Literal["setting_up", "ready", "failed", "stale", "removing"]

PrHeadline = Literal[
    "no_pr",
    "merged",
    "closed",
    "ci_failing",
    "merge_conflicts",
    "in_merge_queue",
    "ready_to_merge",
    "human_comment",
    "review_requested",
    "checks_running",
    "waiting_on_others",
    "draft",
]


class PrChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `passed` rather than `pass` to dodge the Python keyword. Used as
    # both the Python field name and the wire-format key — keeps the
    # backend and frontend reading the same word.
    passed: int = 0
    fail: int = 0
    pending: int = 0
    total: int = 0


class PrComments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    human: int = 0
    bot: int = 0
    total: int = 0


class PrStateSummary(BaseModel):
    """Hub-row payload describing PR state for a single worktree.
    Mirrors backend/app/services/pr_state.py's PrSummary, plus the
    ``checked_at`` timestamp from the cache row."""

    model_config = ConfigDict(extra="forbid")

    headline: PrHeadline
    pr_number: int | None = None
    url: str | None = None
    title: str | None = None
    is_draft: bool = False
    mergeable: str | None = None
    merge_state_status: str | None = None
    review_decision: str | None = None
    checks: PrChecks = Field(default_factory=PrChecks)
    comments: PrComments = Field(default_factory=PrComments)
    base_ref: str | None = None
    head_ref: str | None = None
    updated_at: str | None = None
    checked_at: str


class WorktreeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    name: str
    path: str
    branch: str
    ticket: str | None = None
    pr_number: int | None = None
    pr_repo: str | None = None
    created_at: str
    status: WorktreeStatus
    # True if an iterm_session row with role='claude' exists for this
    # worktree. Populated at read time (not stored in the worktree table)
    # so the hub can render the "claude running" indicator and decide
    # whether skill-runner buttons should be enabled.
    has_claude_session: bool = False
    # Cached PR state from the pr_state polling task (populated by the
    # LEFT JOIN in the list-worktrees query). None when no row has been
    # polled yet — e.g., daemon just started.
    pr_state: PrStateSummary | None = None


def derive_worktree_name(
    branch: str,
    branch_prefix: str = "",
    ticket_pattern: str | None = None,
) -> str:
    """Derive a filesystem-friendly worktree short-name from a branch.

    Steps:
    1. Strip ``branch_prefix`` if it matches the start of the branch.
    2. Replace ``-`` with ``_`` everywhere EXCEPT inside the segment that
       matches ``ticket_pattern`` (so ``TICKET-123`` survives intact).

    Examples (with ``branch_prefix="alice/"``, ``ticket_pattern=r"[A-Z]+-\\d+"``):

      ``alice/TICKET-77_login-flow-fix`` → ``TICKET-77_login_flow_fix``
      ``alice/cleanup-old-foo``          → ``cleanup_old_foo``
      ``main``                           → ``main``
    """
    short = branch
    if branch_prefix and short.startswith(branch_prefix):
        short = short[len(branch_prefix):]

    if not ticket_pattern:
        return short.replace("-", "_")

    pattern = re.compile(ticket_pattern)
    # Find the first ticket-pattern match in the short-name. Anything outside
    # that match gets the hyphen-to-underscore treatment; the match itself
    # passes through unchanged.
    m = pattern.search(short)
    if m is None:
        return short.replace("-", "_")

    head = short[: m.start()].replace("-", "_")
    middle = short[m.start() : m.end()]
    tail = short[m.end() :].replace("-", "_")
    return head + middle + tail


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def extract_ticket(branch: str, ticket_pattern: str | None) -> str | None:
    if not ticket_pattern:
        return None
    m = re.search(ticket_pattern, branch)
    return m.group(0) if m else None
