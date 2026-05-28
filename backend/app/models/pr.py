"""Pydantic shape for a unified PR row.

Mirrors the ``pr`` table created by migration 013. Replaces the
per-surface ``BookmarkRow`` / ``InboxRow`` / ``AuthoredPrRow`` shapes
by keying on GitHub identity ``(pr_repo, pr_number)`` and tracking
which surfaces the row belongs to via the origin booleans.

The legacy per-surface models stay during plan-59 — the shim modules
project ``PrRow`` back to them so routes don't need to change. Plan-61
removes the shims and routes consume ``PrRow`` directly.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.worktree import PrStateSummary

PrCiStatus = Literal["pass", "fail", "pending", "none"]
PrState = Literal["open", "closed", "merged"]


class PrRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_repo: str
    pr_number: int

    is_bookmarked: bool = False
    is_inbox: bool = False
    is_archived: bool = False

    bookmarked_at: str | None = None
    inbox_added_at: str | None = None
    archived_at: str | None = None

    inbox_sources: list[str] = Field(default_factory=list)

    title: str | None = None
    author_login: str | None = None
    url: str | None = None
    ticket: str | None = None
    state: PrState | None = None
    is_draft: bool = False
    ci_status: PrCiStatus | None = None
    pr_updated_at: str | None = None

    notes: str | None = None

    last_seen_at: str | None = None
    last_refreshed_at: str | None = None

    pr_state: PrStateSummary | None = None
