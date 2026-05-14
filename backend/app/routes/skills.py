"""Hub-level "global" skill buttons.

Each button maps to a slash-command Claude skill that isn't bound to a
specific worktree (e.g. ``/pr-check-action-required`` which queries all
the user's open PRs across every repo). Clicking a button spawns a
fresh iTerm2 window at the configured ``cwd`` and launches
``claude /<skill>`` as the initial prompt.

The set of allowed skill names is exactly ``config.global_skills`` —
the config IS the server-side allow-list, so unknown names get a 404
without ever reaching the spawn helper.
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
