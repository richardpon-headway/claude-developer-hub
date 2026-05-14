"""``GET /api/inbox`` — read the latest cached inbox poll result.

The poll loop in :mod:`app.services.inbox_poll` runs every 60s and
writes to ``app.state.inbox``. This endpoint just serializes that.

If the first poll hasn't completed yet (or ``gh`` was missing on the
first attempt), responses come back with ``prs=[]`` and
``checked_at=null`` — the frontend renders a quiet loading state.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.services.inbox_poll import InboxCache, InboxPr

router = APIRouter(prefix="/api", tags=["inbox"])


class InboxPrPayload(BaseModel):
    pr_repo: str
    pr_number: int
    title: str
    author_login: str
    head_ref: str
    base_ref: str
    is_draft: bool
    url: str
    updated_at: str
    ci_status: str
    source: str
    stack_top_pr_number: int | None = None
    stack_size: int
    stack_position: int
    repo_configured: bool


class InboxResponse(BaseModel):
    prs: list[InboxPrPayload]
    checked_at: str | None = None


def _to_payload(pr: InboxPr) -> InboxPrPayload:
    return InboxPrPayload(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=pr.title,
        author_login=pr.author_login,
        head_ref=pr.head_ref,
        base_ref=pr.base_ref,
        is_draft=pr.is_draft,
        url=pr.url,
        updated_at=pr.updated_at,
        ci_status=pr.ci_status,
        source=pr.source,
        stack_top_pr_number=pr.stack_top_pr_number,
        stack_size=pr.stack_size,
        stack_position=pr.stack_position,
        repo_configured=pr.repo_configured,
    )


@router.get("/inbox", response_model=InboxResponse)
async def get_inbox(request: Request) -> InboxResponse:
    cache: InboxCache | None = getattr(request.app.state, "inbox", None)
    if cache is None:
        return InboxResponse(prs=[], checked_at=None)
    return InboxResponse(
        prs=[_to_payload(p) for p in cache.prs],
        checked_at=cache.checked_at,
    )
