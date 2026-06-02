"""Hub-level Claude launchers (not bound to a worktree).

Two endpoints live here, both of which spawn a terminal window in
``config.development_root`` with Claude pre-loaded:

- ``POST /api/skills/global/open`` opens a blank ``claude`` session (no
  prompt) — the equivalent of opening a terminal in your dev root and
  typing ``claude`` yourself.
- ``POST /api/skills/global/freeform`` accepts arbitrary user-typed
  text and spawns Claude with that text as its first message —
  ``claude '<prompt>'``.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.services import terminal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])


class GlobalSkillResponse(BaseModel):
    spawned: bool


# --- generic / free-form spawns ---------------------------------------------


def _development_root() -> Path:
    """Resolve ``config.development_root`` to an existing directory, or
    raise 400. Shared by the blank-session and free-form spawns — both
    open Claude in the user's dev root with no repo/worktree context."""
    config = load_config()
    dev_root = Path(str(config.development_root)).expanduser()
    if not dev_root.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"development_root does not exist on disk: {dev_root}",
        )
    return dev_root


@router.post("/global/open", response_model=GlobalSkillResponse)
async def open_global_claude(request: Request) -> GlobalSkillResponse:
    """Open a fresh Claude session in ``config.development_root`` with
    no initial prompt — a generic "just start Claude" window not tied
    to any repo or worktree. Equivalent to opening a terminal in the
    dev root and typing ``claude`` yourself."""
    dev_root = _development_root()
    await terminal.spawn_one_tab_claude(request, dev_root)
    return GlobalSkillResponse(spawned=True)


class FreeformPromptRequest(BaseModel):
    # Cap the prompt to a reasonable size — the shell-quoted form is
    # what gets passed to `claude '<prompt>'`, and very large prompts
    # are better expressed as a file Claude can read.
    prompt: str = Field(..., min_length=1, max_length=4000)


@router.post("/global/freeform", response_model=GlobalSkillResponse)
async def run_global_freeform(
    req: FreeformPromptRequest, request: Request
) -> GlobalSkillResponse:
    """Open an iTerm2 window at ``config.development_root`` and launch
    Claude with the user-typed ``prompt`` as the initial input.

    No allow-list — this is the same as the user opening a terminal in
    their dev root and typing ``claude '<prompt>'`` themselves. The
    point of the hub button is convenience: one place to fire off ad-hoc
    questions without leaving CDH.
    """
    dev_root = _development_root()
    await terminal.spawn_one_tab_claude(request, dev_root, req.prompt)
    return GlobalSkillResponse(spawned=True)
