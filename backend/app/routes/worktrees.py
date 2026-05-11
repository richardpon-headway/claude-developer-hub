"""REST endpoints for the worktree CRUD slice + the iTerm2 spawn endpoint.

Delete / retry-from-step / force-remove come later when the workspace
page needs them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.models.worktree import WorktreeRow
from app.services import worktree as svc
from app.services.iterm_spawn import (
    SpawnResult,
    spawn_worktree_window,
    upsert_iterm_sessions_sync,
)

router = APIRouter(prefix="/api", tags=["worktrees"])


class CreateWorktreeRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    branch: str = Field(..., min_length=1)


class WorktreeDetail(BaseModel):
    row: WorktreeRow
    log: list[str]


@router.post("/worktree", response_model=WorktreeRow)
async def create_worktree(req: CreateWorktreeRequest) -> WorktreeRow:
    try:
        return await svc.create_worktree(req.repo, req.branch)
    except svc.WorktreeCreationError as e:
        msg = str(e)
        # "already exists" / "name collision" → 409. Otherwise 400.
        code = status.HTTP_409_CONFLICT if "already exists" in msg else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, msg) from e


@router.get("/worktrees", response_model=list[WorktreeRow])
async def list_worktrees() -> list[WorktreeRow]:
    return await asyncio.to_thread(svc.list_worktrees_sync)


@router.get("/worktree/{repo}/{name}", response_model=WorktreeDetail)
async def get_worktree(repo: str, name: str) -> WorktreeDetail:
    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}")
    return WorktreeDetail(row=row, log=svc.get_log(repo, name))


class SpawnItermResponse(BaseModel):
    window_id: str
    claude_session_id: str
    shell_session_id: str


@router.post("/worktree/{repo}/{name}/spawn-iterm", response_model=SpawnItermResponse)
async def spawn_iterm(repo: str, name: str, request: Request) -> SpawnItermResponse:
    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API "
            "and approve the first-connection auth dialog, then wait a few seconds.",
        )

    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}")

    worktree_path = Path(row.path)
    if not worktree_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"worktree path missing on disk: {worktree_path}",
        )

    frame = load_config().iterm2.default_window
    try:
        result: SpawnResult = await spawn_worktree_window(iterm.connection, worktree_path, frame)
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    await asyncio.to_thread(upsert_iterm_sessions_sync, repo, name, result)

    return SpawnItermResponse(
        window_id=result.window_id,
        claude_session_id=result.claude_session_id,
        shell_session_id=result.shell_session_id,
    )
