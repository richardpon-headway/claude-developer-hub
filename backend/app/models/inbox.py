"""Pydantic shape for an inbox row.

Mirrors the ``inbox`` table in migrations/009_persistent_inbox_bookmarks.sql.
Used by the REST layer to serialize rows and by the service layer to
type its returns.

Inbox rows are persistent: once a PR enters via ``gh search prs`` it
stays until either the PR closes / merges (auto-removal sweep, in
``app.services.inbox_poll``) or the user explicitly archives it
(``inbox_archived`` table). Notes, ticket, and the workspace card
chrome render the same way they do for worktree-backed rows.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

InboxCiStatus = Literal["pass", "fail", "pending", "none"]


class InboxRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_repo: str  # GitHub "owner/name"
    pr_number: int
    title: str
    author_login: str
    url: str
    is_draft: bool
    ci_status: InboxCiStatus
    # Priority-ordered; sources[0] is the highest-priority signal that
    # put this PR in the inbox (used by the row chip layout).
    sources: list[str]
    notes: str | None = None
    ticket: str | None = None
    pr_updated_at: str
    added_at: str
    last_seen_at: str
