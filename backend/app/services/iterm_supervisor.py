"""Long-lived asyncio task that owns the iTerm2 connection.

Slice A ships a placeholder that idles. Slice F wires up
``iterm2.Connection.async_create()``, notification subscriptions for
session/window lifecycle, exponential-backoff reconnect, and the
``iterm2_started_at`` restart probe.
"""
from __future__ import annotations

import asyncio
from typing import Any


async def iterm_supervisor(state: Any) -> None:
    while True:
        await asyncio.sleep(3600)
