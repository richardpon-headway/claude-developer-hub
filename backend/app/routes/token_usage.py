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

import asyncio
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
    # Calendar-day totals from CTM's `/api/usage/windows.today_local`.
    # Matches the "Today" tile in the CTM dashboard.
    today_output: int = 0
    today_input: int = 0
    today_messages: int = 0
    # Topic breakdown is a trailing-24h window, since CTM's groups
    # endpoint doesn't expose calendar-day granularity. The frontend
    # labels this explicitly so it's not confused with the today totals.
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
    """Fetch CTM's calendar-day totals + last-24h topic groups.

    Two CTM calls in parallel:

    - ``/api/usage/windows`` for ``today_local`` (the calendar-day
      output/input/messages that match CTM's "Today" tile).
    - ``/api/usage/groups?range=1d&by=topic`` for the per-topic
      breakdown (no calendar-day option exists; trailing 24h is the
      finest available granularity).

    ``offline: True`` is a normal response when either call fails —
    not an error — so the frontend renders a simple offline badge
    rather than a fetch-error spinner.
    """
    config = load_config()
    base = config.token_monitor.api_url.rstrip("/")
    windows_url = f"{base}/api/usage/windows"
    groups_url = f"{base}/api/usage/groups?range=1d&by=topic"

    try:
        async with httpx.AsyncClient(timeout=CTM_TIMEOUT_SECONDS) as client:
            windows_resp, groups_resp = await asyncio.gather(
                client.get(windows_url),
                client.get(groups_url),
            )
            windows_resp.raise_for_status()
            groups_resp.raise_for_status()
            windows_data = windows_resp.json()
            groups_data = groups_resp.json()
    except Exception as e:
        log.info("token-monitor unreachable at %s: %s", base, e)
        return TokenUsageResponse(offline=True, rows=[])

    today = windows_data.get("today_local") or {}
    raw_rows = groups_data.get("rows", [])
    trimmed = [
        TokenUsageRow(**{k: v for k, v in row.items() if k in _RELEVANT_FIELDS})
        for row in raw_rows
    ]
    return TokenUsageResponse(
        offline=False,
        today_output=int(today.get("output") or 0),
        today_input=int(today.get("input") or 0),
        today_messages=int(today.get("messages") or 0),
        rows=trimmed,
    )
