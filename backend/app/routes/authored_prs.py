"""Authored-PR HTTP endpoints.

The "My PRs (no worktree)" tier on the hub: PRs the user authored
that are still open and don't already have a local worktree / inbox
row / bookmark. Read from the unified ``pr`` table with
``author_login=@me``; discovery is :mod:`app.services.authored_poll`'s
job.

- ``GET /api/authored-prs`` — list rows (with attached notes).
- ``POST /api/authored-prs/{pr_repo}/{pr_number}/pull-down`` —
  delegates to the inbox route's ``_perform_pull_down`` with the
  resolved ``@me`` login set as the worktree's author. No 404 guard
  against URL guessing — localhost-only and the configured-repo
  check still gates which repos can be pulled down.
- ``PUT /api/authored-prs/{pr_repo}/{pr_number}/notes`` — upsert into
  ``pr.notes``. Notes survive across polls; on surface transition
  (bookmark, pull-down) they migrate via ``pr.notes`` itself since
  the column is shared across surfaces.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.models.pr import PrCiStatus
from app.models.worktree import now_iso
from app.services import pr_db
from app.services.gh_identity import get_user_login
from app.services.inbox_search import (
    configured_repos_index,
    extract_ticket,
    is_repo_configured,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["authored_prs"])


# Soft cap matching the other notes endpoints (inbox + bookmark + worktree).
_NOTES_MAX_LENGTH = 10_000


class AuthoredPr(BaseModel):
    pr_repo: str
    pr_number: int
    title: str
    url: str
    is_draft: bool
    ci_status: PrCiStatus
    ticket: str | None = None
    pr_updated_at: str
    repo_configured: bool
    notes: str | None = None


class AuthoredPrListResponse(BaseModel):
    authored_prs: list[AuthoredPr]


@router.get("/authored-prs", response_model=AuthoredPrListResponse)
async def list_authored_prs() -> AuthoredPrListResponse:
    """Return the user's open authored PRs from the unified pr table.

    Falls back to an empty list when ``gh`` is unauthed (no local
    login resolved) — same fail-open contract as the legacy path.
    """
    try:
        rows = await _list_authored()
    except Exception as e:  # pragma: no cover — defensive
        log.warning("authored-prs read failed: %s; returning empty", e)
        return AuthoredPrListResponse(authored_prs=[])
    return AuthoredPrListResponse(authored_prs=rows)


async def _list_authored() -> list[AuthoredPr]:
    local_login = await get_user_login()
    if local_login is None:
        return []

    config = load_config()
    repos_index = configured_repos_index(config.repos)

    rows = await asyncio.to_thread(
        pr_db.list_pr_sync,
        author_login=local_login,
        state="open",
        is_bookmarked=False,
        is_inbox=False,
        has_worktree=False,
        order_by="pr.pr_updated_at DESC",
    )

    out: list[AuthoredPr] = []
    for r in rows:
        out.append(
            AuthoredPr(
                pr_repo=r.pr_repo,
                pr_number=r.pr_number,
                title=r.title or "",
                url=r.url or "",
                is_draft=r.is_draft,
                ci_status=r.ci_status or "none",
                ticket=r.ticket or extract_ticket(r.title or "", config.repos),
                pr_updated_at=r.pr_updated_at or "",
                repo_configured=is_repo_configured(r.pr_repo, repos_index),
                notes=r.notes,
            )
        )
    return out


# Pull-down for authored-PR rows. Imported lazily to avoid pulling in
# the inbox route's transitive deps at module load.
from app.routes.inbox import PullDownResponse, _perform_pull_down  # noqa: E402


@router.post(
    "/authored-prs/{pr_repo:path}/{pr_number}/pull-down",
    response_model=PullDownResponse,
)
async def pull_down_authored(pr_repo: str, pr_number: int) -> PullDownResponse:
    """Pull-down for an authored PR. Resolves the user's gh login so
    the new worktree row knows the PR is owned by the user (vs. a
    teammate's PR pulled down for review). ``get_user_login`` returns
    None when ``gh`` is unauthed; the pr_state poll backfills the
    column later in that case."""
    user_login = await get_user_login()
    return await _perform_pull_down(
        pr_repo, pr_number, author_login=user_login
    )


# ---------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------


class UpdateNotesRequest(BaseModel):
    notes: str = Field(..., max_length=_NOTES_MAX_LENGTH)


class UpdateNotesResponse(BaseModel):
    notes: str


@router.put(
    "/authored-prs/{pr_repo:path}/{pr_number}/notes",
    response_model=UpdateNotesResponse,
)
async def update_notes(
    pr_repo: str, pr_number: int, req: UpdateNotesRequest
) -> UpdateNotesResponse:
    """Upsert the note for an authored-PR row.

    No 404 — authored rows may not yet have been written by the
    discovery poll, so any ``(pr_repo, pr_number)`` is a valid target.
    The upsert path inserts a stub pr row if none exists so the note
    survives until the next poll fills in the rest of the columns.
    Empty string is a valid value; it clears the visible note while
    keeping the row tracked so surface-transition handlers see it.
    """
    if pr_number <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "pr_number must be positive"
        )
    await asyncio.to_thread(
        pr_db.upsert_notes_sync,
        pr_repo, pr_number, req.notes, now_iso(),
    )
    return UpdateNotesResponse(notes=req.notes)
