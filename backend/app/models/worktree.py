"""Pydantic shape for a worktree row.

Mirrors the ``worktree`` table in migrations/001_initial.sql. Used by the
REST layer to serialize rows and by the service layer to type its returns.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorktreeStatus = Literal["setting_up", "ready", "failed", "stale", "removing"]


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
    return datetime.now(timezone.utc).isoformat()


def extract_ticket(branch: str, ticket_pattern: str | None) -> str | None:
    if not ticket_pattern:
        return None
    m = re.search(ticket_pattern, branch)
    return m.group(0) if m else None
