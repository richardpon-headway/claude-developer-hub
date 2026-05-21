"""Authored-PR HTTP endpoints (plan-48, Slice C).

The "My PRs (no worktree)" tier on the hub: PRs the user authored
that are still open and don't already have a local worktree / inbox
row / bookmark. Computed fresh on each request — nothing persisted.

- ``GET /api/authored-prs`` — list rows.
- ``POST /api/authored-prs/{pr_repo}/{pr_number}/pull-down`` —
  delegates to the inbox route's ``_perform_pull_down`` with the
  resolved ``@me`` login set as the worktree's author. No 404 guard
  against URL guessing — localhost-only and the configured-repo
  check still gates which repos can be pulled down.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.models.authored_pr import AuthoredPrRow
from app.services.authored_prs import fetch_authored_prs_safe
from app.services.gh_identity import get_user_login

router = APIRouter(prefix="/api", tags=["authored_prs"])


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
