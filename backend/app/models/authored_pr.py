"""Pydantic shape for an authored-PR row (no worktree).

Not persisted — recomputed each list request from ``gh search prs
--author:@me --state open``. Renders as a top tier in the
``WorkspaceList`` so the user sees their own in-flight PRs without
needing a local worktree, and can pull each down with one click.

When the PR closes or merges it drops from the ``--state open``
filter and the tier naturally clears — no archive UI needed.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

AuthoredCiStatus = Literal["pass", "fail", "pending", "none"]


class AuthoredPrRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_repo: str  # GitHub "owner/name"
    pr_number: int
    title: str
    url: str
    is_draft: bool
    ci_status: AuthoredCiStatus
    ticket: str | None = None
    pr_updated_at: str
    # True when this PR's repo maps to a locally-configured RepoConfig.
    # The frontend uses this to render "Pull down" vs "Configure repo
    # + pull down".
    repo_configured: bool
    # Free-form per-PR notes from the ``authored_pr_notes`` table.
    # NULL when no row exists. On surface transition (bookmark, pull-
    # down) the route handler copies these into the destination
    # surface's notes column and deletes the row here.
    notes: str | None = None
