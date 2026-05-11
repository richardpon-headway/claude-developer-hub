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
from app.services.iterm_send import (
    SendGateError,
    SessionNotFoundError,
    send_to_session,
)
from app.services.iterm_spawn import (
    SpawnResult,
    get_claude_session_id_sync,
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


# --- send-text / run-skill -----------------------------------------------


class SendTextRequest(BaseModel):
    text: str = Field(..., min_length=1)
    press_enter: bool = True


class RunSkillRequest(BaseModel):
    # Slash-command names are kebab-case lowercase per Claude Code's
    # convention. Reject anything that wouldn't be a valid skill name.
    skill_name: str = Field(..., min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")


class SendResponse(BaseModel):
    sent: bool


async def _send_to_worktree_claude(
    request: Request, repo: str, name: str, text: str, press_enter: bool
) -> SendResponse:
    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API.",
        )

    claude_sid = await asyncio.to_thread(get_claude_session_id_sync, repo, name)
    if claude_sid is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"no Claude iTerm2 session for {repo}/{name} — open it in iTerm2 first",
        )

    try:
        await send_to_session(iterm.connection, claude_sid, text, press_enter=press_enter)
    except SessionNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except SendGateError as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Claude is awaiting input (matched {e.matched_pattern!r}). "
            "Resolve the prompt first.",
        ) from e

    return SendResponse(sent=True)


@router.post("/worktree/{repo}/{name}/send-text", response_model=SendResponse)
async def send_text(
    repo: str, name: str, req: SendTextRequest, request: Request
) -> SendResponse:
    return await _send_to_worktree_claude(request, repo, name, req.text, req.press_enter)


@router.post("/worktree/{repo}/{name}/run-skill", response_model=SendResponse)
async def run_skill(
    repo: str, name: str, req: RunSkillRequest, request: Request
) -> SendResponse:
    # Slash command always carries Enter so Claude processes it as a
    # complete invocation rather than sitting at a half-typed prompt.
    return await _send_to_worktree_claude(
        request, repo, name, f"/{req.skill_name}", press_enter=True
    )
