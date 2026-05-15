"""Hub-level "global" skill buttons + free-form prompt entry.

Two related endpoints live here, both of which spawn an iTerm2 window
with Claude pre-loaded:

- ``POST /api/skills/global`` runs a named skill (a slash command in
  ``config.global_skills`` — the allow-list lives in user config).
- ``POST /api/skills/global/freeform`` accepts arbitrary user-typed
  text and spawns Claude in ``config.development_root`` with that text
  as Claude's first message. No allow-list: it's the equivalent of
  opening a terminal and typing ``claude '<prompt>'`` yourself.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.config.schema import GlobalSkill
from app.services.iterm_spawn import spawn_global_claude_window

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])


class GlobalSkillRequest(BaseModel):
    skill: str = Field(..., min_length=1, max_length=64)


class GlobalSkillResponse(BaseModel):
    window_id: str
    claude_session_id: str


def _resolve_cwd(skill: GlobalSkill) -> Path:
    if skill.cwd == "home":
        return Path.home()
    p = Path(skill.cwd).expanduser()
    if not p.is_absolute():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"global_skills[{skill.name}].cwd must be 'home' or an absolute path",
        )
    if not p.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"global_skills[{skill.name}].cwd does not exist: {p}",
        )
    return p


@router.post("/global", response_model=GlobalSkillResponse)
async def run_global_skill(
    req: GlobalSkillRequest, request: Request
) -> GlobalSkillResponse:
    """Open an iTerm2 window and launch Claude with the skill's slash
    command as initial input. ``req.skill`` must match one of the
    ``global_skills`` entries in the user's config — that's the
    allow-list."""
    config = load_config()
    skill = next((s for s in config.global_skills if s.name == req.skill), None)
    if skill is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown global skill: {req.skill!r}. Add it to "
            "`global_skills` in ~/.config/cdh/config.yaml.",
        )

    cwd = _resolve_cwd(skill)

    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API "
            "and approve the first-connection auth dialog, then wait a few seconds.",
        )

    frame = config.iterm2.default_window
    try:
        result = await spawn_global_claude_window(
            iterm.connection, cwd, frame, f"/{skill.name}"
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    return GlobalSkillResponse(
        window_id=result.window_id,
        claude_session_id=result.claude_session_id,
    )


# --- free-form prompt --------------------------------------------------------


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
    config = load_config()
    dev_root = Path(str(config.development_root)).expanduser()
    if not dev_root.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"development_root does not exist on disk: {dev_root}",
        )

    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API "
            "and approve the first-connection auth dialog, then wait a few seconds.",
        )

    frame = config.iterm2.default_window
    try:
        result = await spawn_global_claude_window(
            iterm.connection, dev_root, frame, req.prompt
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    return GlobalSkillResponse(
        window_id=result.window_id,
        claude_session_id=result.claude_session_id,
    )
