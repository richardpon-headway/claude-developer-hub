"""Authored-PR HTTP endpoints.

The authored PRs themselves surface through the unified
``GET /api/workspaces`` list; this module keeps the per-PR actions:

- ``POST /api/authored-prs/{pr_repo}/{pr_number}/pull-down`` —
  delegates to the shared ``perform_pull_down`` engine with the
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

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models.worktree import now_iso
from app.services import pr_db
from app.services.gh_identity import get_user_login
from app.services.pull_down import PullDownResponse, perform_pull_down

router = APIRouter(prefix="/api", tags=["authored_prs"])


# Soft cap matching the other notes endpoints (bookmark + worktree).
_NOTES_MAX_LENGTH = 10_000


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
    return await perform_pull_down(
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
