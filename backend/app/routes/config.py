"""Read-only views into the user's local CDH config.

These endpoints expose only the user-facing fields the frontend needs to
render. Internal fields (``server.host``/``port``, sidecar paths) stay
server-side.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.config.loader import load_config
from app.config.schema import JiraConfig

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/jira", response_model=JiraConfig)
async def get_jira_config() -> JiraConfig:
    return load_config().jira
