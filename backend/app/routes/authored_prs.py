"""Authored-PR HTTP endpoints (plan-48 Slice C + plan-50).

The "My PRs (no worktree)" tier on the hub: PRs the user authored
that are still open and don't already have a local worktree / inbox
row / bookmark. Computed fresh on each request — but per-row notes
are persisted in the ``authored_pr_notes`` table.

- ``GET /api/authored-prs`` — list rows (with attached notes).
- ``POST /api/authored-prs/{pr_repo}/{pr_number}/pull-down`` —
  delegates to the inbox route's ``_perform_pull_down`` with the
  resolved ``@me`` login set as the worktree's author. No 404 guard
  against URL guessing — localhost-only and the configured-repo
  check still gates which repos can be pulled down.
- ``PUT /api/authored-prs/{pr_repo}/{pr_number}/notes`` — upsert into
  ``authored_pr_notes``. Notes survive across polls (PR drops out of
  the search results only when closed/merged, at which point the
  notes row becomes orphaned — see plan-50 out-of-scope).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models.authored_pr import AuthoredPrRow
from app.models.worktree import now_iso
from app.services import authored_pr_notes_db
from app.services.authored_prs import fetch_authored_prs_safe
from app.services.gh_identity import get_user_login

router = APIRouter(prefix="/api", tags=["authored_prs"])


# Soft cap matching the other notes endpoints (inbox + bookmark + worktree).
_NOTES_MAX_LENGTH = 10_000


class AuthoredPrListResponse(BaseModel):
    authored_prs: list[AuthoredPrRow]


@router.get("/authored-prs", response_model=AuthoredPrListResponse)
async def list_authored_prs() -> AuthoredPrListResponse:
    rows = await fetch_authored_prs_safe()
    return AuthoredPrListResponse(authored_prs=rows)


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

    No 404 — authored rows aren't persisted, so any ``(pr_repo,
    pr_number)`` is a valid target. Empty string is a valid value;
    it clears the visible note while keeping the row tracked so
    surface-transition handlers know to migrate it.
    """
    if pr_number <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "pr_number must be positive"
        )
    await asyncio.to_thread(
        authored_pr_notes_db.upsert_notes_sync,
        pr_repo, pr_number, req.notes, now_iso(),
    )
    return UpdateNotesResponse(notes=req.notes)
