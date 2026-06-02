"""Read-only views into the user's local CDH config.

These endpoints expose only the user-facing fields the frontend needs to
render. Internal fields (``server.host``/``port``, sidecar paths) stay
server-side.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config.loader import load_config
from app.config.schema import DiffConfig, JiraConfig
from app.services import terminal

router = APIRouter(prefix="/api/config", tags=["config"])


class TerminalInfo(BaseModel):
    """User-visible terminal info — kind + human-readable display
    name. Frontend uses ``display_name`` to label "Open in <X>"
    buttons without hardcoding a terminal."""

    kind: str
    display_name: str


@router.get("/terminal", response_model=TerminalInfo)
async def get_terminal_info() -> TerminalInfo:
    kind = terminal.active_kind()
    return TerminalInfo(kind=kind, display_name=terminal.display_name(kind))


@router.get("/jira", response_model=JiraConfig)
async def get_jira_config() -> JiraConfig:
    return load_config().jira


@router.get("/diff", response_model=DiffConfig)
async def get_diff_config() -> DiffConfig:
    """Diff-view rendering knobs (context lines, expand-all threshold).
    The frontend reads these to set the collapse default per file."""
    return load_config().diff
