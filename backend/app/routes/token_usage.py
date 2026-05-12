"""Hub token-usage tile — proxy claude-token-monitor's groups endpoint.

Why proxy rather than letting the frontend fetch CTM directly:

- One backend port to remember (the vite dev proxy is already wired for /api).
- The CTM rows are large (each row carries a ``sample_prompts`` array
  with multi-KB strings). We strip those server-side; the hub tile only
  needs totals and labels.
- The frontend can branch on a clean ``{offline: true}`` response
  rather than having to interpret network failures.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from app.config.loader import load_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["token-usage"])

CTM_TIMEOUT_SECONDS = 2.0


class TokenUsageRow(BaseModel):
    topic_id: str
    sessions: int
    output: int
    input: int
    messages: int
    last_at: str | None = None
    label: str | None = None
    summary: str | None = None


class TokenUsageResponse(BaseModel):
    offline: bool
    rows: list[TokenUsageRow]


_RELEVANT_FIELDS = {
    "topic_id",
    "sessions",
    "output",
    "input",
    "messages",
    "last_at",
    "label",
    "summary",
}


@router.get("/token-usage", response_model=TokenUsageResponse)
async def token_usage() -> TokenUsageResponse:
    """Fetch CTM's last-day topic groups and return a trimmed view.

    ``offline: True`` is a normal response when CTM is unreachable —
    not an error — so the frontend renders a simple offline badge
    rather than a fetch-error spinner.
    """
    config = load_config()
    url = (
        f"{config.token_monitor.api_url.rstrip('/')}/api/usage/groups"
        "?range=1d&by=topic"
    )
    try:
        async with httpx.AsyncClient(timeout=CTM_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        log.info("token-monitor unreachable at %s: %s", url, e)
        return TokenUsageResponse(offline=True, rows=[])

    raw_rows = data.get("rows", [])
    trimmed = [
        TokenUsageRow(**{k: v for k, v in row.items() if k in _RELEVANT_FIELDS})
        for row in raw_rows
    ]
    return TokenUsageResponse(offline=False, rows=trimmed)
