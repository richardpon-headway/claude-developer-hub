"""Manual refresh endpoints for the background polling loops.

Two sibling endpoints, both triggering a single synchronous ``_tick``
of their respective loop. Used by the hub's Sync button so the user
doesn't have to wait for the next background interval. Failures
inside the tick are swallowed by the same handler as the background
path, so the endpoints are safe to call even when ``gh`` is
misbehaving.

FE wire-up to the Sync button is deferred to a follow-up plan; these
endpoints are smoke-tested via curl in the verification step.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import authored_poll, pr_enrichment_poll

router = APIRouter(prefix="/api", tags=["refresh"])


class RefreshEnrichmentResponse(BaseModel):
    refreshed: int


class RefreshOkResponse(BaseModel):
    ok: bool = True


@router.post("/refresh-authored", response_model=RefreshOkResponse)
async def refresh_authored() -> RefreshOkResponse:
    """Force an immediate authored-poll tick. The refreshed rows surface
    via the unified ``GET /api/workspaces`` list."""
    await authored_poll._tick()
    return RefreshOkResponse()


@router.post("/refresh-enrichment", response_model=RefreshEnrichmentResponse)
async def refresh_enrichment() -> RefreshEnrichmentResponse:
    """Force an immediate enrichment tick. Returns the count of pr
    rows visited; useful as a smoke test for the manual refresh
    button when wired up later."""
    targets = pr_enrichment_poll._list_enrichment_targets_sync()
    await pr_enrichment_poll._tick()
    return RefreshEnrichmentResponse(refreshed=len(targets))
