"""Pydantic shape for a unified PR row.

Mirrors the ``pr`` table. Keys on GitHub identity
``(pr_repo, pr_number)``. The ``is_bookmarked`` origin flag marks
rows the user manually bookmarked; authored rows are identified by
``author_login`` rather than a flag.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.worktree import PrStateSummary

PrCiStatus = Literal["pass", "fail", "pending", "none"]
PrState = Literal["open", "closed", "merged"]


class PrRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_repo: str
    pr_number: int

    is_bookmarked: bool = False
    bookmarked_at: str | None = None

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
