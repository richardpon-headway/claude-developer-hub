"""REST endpoints for the worktree CRUD slice.

Only create + list + detail are wired here in this slice. Delete /
retry-from-step / force-remove come later when the workspace page needs
them.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models.worktree import WorktreeRow
from app.services import worktree as svc

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
