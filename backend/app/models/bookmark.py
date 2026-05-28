"""Pydantic shape for a bookmark row.

Mirrors the ``bookmark`` table in migrations/009_persistent_inbox_bookmarks.sql.

Bookmarks are manually-added PR watches: the user pastes a GitHub PR
URL into the hub and we persist a row with the PR's metadata. Unlike
inbox rows, bookmarks are never auto-removed when the PR closes or
merges — the row stays (with a ``closed`` / ``merged`` chip) until
the user explicitly unbookmarks it.

The background ``pr_enrichment_poll`` task refreshes ``state``,
``title``, ``author_login``, and ``last_refreshed_at`` via ``gh pr
view`` so the card stays current; ``notes`` and ``bookmarked_at`` are
user-owned and never touched by the poller.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

BookmarkState = Literal["open", "closed", "merged"]


class BookmarkRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_repo: str  # GitHub "owner/name"
    pr_number: int
    title: str
    author_login: str
    url: str
    state: BookmarkState
    notes: str | None = None
    ticket: str | None = None
    bookmarked_at: str
    last_refreshed_at: str
