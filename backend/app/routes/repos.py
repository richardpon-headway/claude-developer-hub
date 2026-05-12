"""Repo management endpoints — list configured repos + Claude-driven onboarding.

Onboarding is a three-step handoff (see plan §7):

1. ``POST /api/repos/onboard {path}`` validates the path, mints a session_id,
   and returns a copy-pasteable prompt for a separate Claude Code terminal
   session to run.
2. The user pastes the prompt into Claude. Claude inspects the repo and
   POSTs the proposed entry back here.
3. ``POST /api/repos/onboard/complete {session_id, proposed_entry}`` validates
   the proposed entry against ``RepoConfig`` and appends it to the on-disk
   config atomically.

The session_id correlation decouples Claude's terminal session from the
FastAPI process and from the browser tab that started the onboarding — the
"Add repo" UI can poll ``GET /api/repos/onboard/{session_id}`` (or, later,
subscribe to an SSE channel) to know when Claude finishes.

Sessions live in process memory only. They have a 5-minute TTL so a stale
session_id can't be replayed long after the user gave up.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config.loader import load_config, save_config
from app.config.schema import RepoConfig

router = APIRouter(prefix="/api/repos", tags=["repos"])

_ONBOARD_TTL_SECONDS = 5 * 60

_lock = asyncio.Lock()
_sessions: dict[str, _OnboardSession] = {}


class _OnboardSession:
    __slots__ = ("session_id", "path", "prompt", "created_at", "state", "proposed_entry", "error")

    def __init__(self, session_id: str, path: Path, prompt: str) -> None:
        self.session_id = session_id
        self.path = path
        self.prompt = prompt
        self.created_at = time.monotonic()
        self.state: Literal["pending", "saved", "error"] = "pending"
        self.proposed_entry: RepoConfig | None = None
        self.error: str | None = None

    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > _ONBOARD_TTL_SECONDS


def _evict_expired() -> None:
    expired = [sid for sid, s in _sessions.items() if s.is_expired()]
    for sid in expired:
        _sessions.pop(sid, None)


def _build_inspection_prompt(path: Path, session_id: str, callback_url: str) -> str:
    return (
        f"Inspect the git repo at `{path}` and propose a CDH config entry for it.\n\n"
        "Detect each of:\n"
        "  - `setup_steps`: read mise.toml, package.json, Makefile, pyproject.toml, "
        "requirements.txt, etc. Infer the commands a fresh checkout needs to run "
        "(`pnpm install`, `uv sync`, `make install`, …). Each step is a "
        "`{cmd, cwd}` pair where `cwd` is relative to the worktree root "
        "(empty string = worktree root itself).\n"
        "  - `branch_prefix`: from recent local branch names, look for a common "
        "author prefix like `<user>/`. Default to `\"\"` if no consistent prefix.\n"
        "  - `ticket_pattern`: from recent commit subjects, look for a recurring "
        "ticket-key regex (e.g. `[A-Z]+-\\d+`). Default `null` if none found.\n"
        "  - `default_branch`: `git symbolic-ref refs/remotes/origin/HEAD` (strip "
        "the `refs/remotes/origin/` prefix), or `main` as a fallback.\n\n"
        "When done, POST your proposal as JSON to:\n"
        f"  {callback_url}\n\n"
        "Body shape:\n"
        "```json\n"
        "{\n"
        f'  "session_id": "{session_id}",\n'
        '  "proposed_entry": {\n'
        '    "name": "<unique slug, lower-case alnum + - and _>",\n'
        f'    "path": "{path}",\n'
        '    "default_branch": "main",\n'
        '    "branch_prefix": "",\n'
        '    "setup_steps": [{"cmd": "...", "cwd": "..."}],\n'
        '    "ticket_pattern": null\n'
        '  }\n'
        "}\n"
        "```\n"
    )


class OnboardRequest(BaseModel):
    path: str = Field(..., min_length=1)


class OnboardResponse(BaseModel):
    session_id: str
    prompt: str


class OnboardStatus(BaseModel):
    session_id: str
    state: Literal["pending", "saved", "error"]
    proposed_entry: RepoConfig | None = None
    error: str | None = None


class OnboardCompleteRequest(BaseModel):
    session_id: str
    proposed_entry: RepoConfig


class OnboardCompleteResponse(BaseModel):
    state: Literal["saved"]
    saved_entry: RepoConfig


def _normalize_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "path must be absolute",
        )
    resolved = p.resolve()
    if not resolved.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"path does not exist or is not a directory: {resolved}",
        )
    if not (resolved / ".git").exists():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"path is not a git repository (no .git found): {resolved}",
        )
    return resolved


@router.get("", response_model=list[RepoConfig])
async def list_repos() -> list[RepoConfig]:
    return load_config().repos


class RepoCandidate(BaseModel):
    path: str
    name: str
    already_configured: bool


def _looks_like_main_checkout(entry: Path) -> bool:
    """True if ``entry/.git`` looks like a main-checkout gitdir (not a
    worktree). Handles three sub-cases:

    - ``.git`` as a regular file → worktree pointer (``gitdir: …``).
      Returns False.
    - ``.git`` as a directory (possibly via symlink) → main checkout
      unless the resolved path is inside another repo's
      ``.git/worktrees/`` segment, in which case it's a manually-
      symlinked worktree.
    - Anything else (broken symlink, special file) → False.
    """
    git_path = entry / ".git"
    if not git_path.exists():
        return False
    if git_path.is_file():
        return False
    if not git_path.is_dir():
        return False
    try:
        resolved = git_path.resolve()
    except OSError:
        return False
    parts = resolved.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".git" and parts[i + 1] == "worktrees":
            return False
    return True


@router.get("/candidates", response_model=list[RepoCandidate])
async def list_candidates() -> list[RepoCandidate]:
    """Auto-discover git main checkouts under ``config.development_root``.

    One level deep, hidden dirs skipped, worktrees excluded (both the
    standard ``.git``-file form and rare manually-symlinked variants).
    Each candidate is flagged ``already_configured`` against the current
    ``config.repos[]``.

    Sort: not-configured first (so onboarding candidates float to the
    top), then alphabetical by name.
    """
    config = load_config()
    dev_root = Path(str(config.development_root)).expanduser()
    if not dev_root.is_dir():
        return []

    configured = {str(r.path) for r in config.repos}

    candidates: list[RepoCandidate] = []
    try:
        entries = list(dev_root.iterdir())
    except OSError:
        return []

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name.startswith("."):
            continue
        try:
            if not _looks_like_main_checkout(entry):
                continue
        except OSError:
            continue
        candidates.append(
            RepoCandidate(
                path=str(entry),
                name=entry.name,
                already_configured=str(entry) in configured,
            )
        )

    candidates.sort(key=lambda c: (c.already_configured, c.name.lower()))
    return candidates


@router.post("/onboard", response_model=OnboardResponse)
async def onboard(req: OnboardRequest) -> OnboardResponse:
    path = _normalize_path(req.path)

    config = load_config()
    for existing in config.repos:
        if existing.path == path:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"a repo at this path is already configured (name={existing.name})",
            )

    callback_url = f"http://{config.server.host}:{config.server.port}/api/repos/onboard/complete"

    async with _lock:
        _evict_expired()
        session_id = secrets.token_urlsafe(16)
        prompt = _build_inspection_prompt(path, session_id, callback_url)
        _sessions[session_id] = _OnboardSession(session_id, path, prompt)

    return OnboardResponse(session_id=session_id, prompt=prompt)


@router.get("/onboard/{session_id}", response_model=OnboardStatus)
async def onboard_status(session_id: str) -> OnboardStatus:
    async with _lock:
        _evict_expired()
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "unknown or expired session_id",
        )
    return OnboardStatus(
        session_id=session.session_id,
        state=session.state,
        proposed_entry=session.proposed_entry,
        error=session.error,
    )


@router.post("/onboard/complete", response_model=OnboardCompleteResponse)
async def onboard_complete(req: OnboardCompleteRequest) -> OnboardCompleteResponse:
    async with _lock:
        _evict_expired()
        session = _sessions.get(req.session_id)
        if session is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "unknown or expired session_id",
            )

        config = load_config()
        for existing in config.repos:
            if existing.name == req.proposed_entry.name:
                session.state = "error"
                session.error = f"name collision: {existing.name}"
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"a repo with this name is already configured: {existing.name}",
                )
            if existing.path == req.proposed_entry.path:
                session.state = "error"
                session.error = f"path collision: {existing.path}"
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"a repo at this path is already configured (name={existing.name})",
                )

        config.repos.append(req.proposed_entry)
        save_config(config)
        session.state = "saved"
        session.proposed_entry = req.proposed_entry

    return OnboardCompleteResponse(state="saved", saved_entry=req.proposed_entry)
