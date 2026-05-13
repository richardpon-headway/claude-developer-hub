"""REST endpoints for the worktree CRUD slice + the iTerm2 spawn endpoint.

Delete / retry-from-step / force-remove come later when the workspace
page needs them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.models.worktree import PrStateSummary, WorktreeRow
from app.services import worktree as svc
from app.services.iterm_send import (
    SendGateError,
    SessionNotFoundError,
    send_to_session,
)
from app.services.iterm_spawn import (
    SpawnResult,
    get_claude_session_id_sync,
    set_iterm_session_uuid_sync,
    spawn_worktree_window,
    upsert_iterm_sessions_sync,
)
from app.services.sidecar import (
    build_sidecar,
    discover_session_id,
    write_sidecar_sync,
)
from app.services.worktree_import import discover_all_sync

log = logging.getLogger(__name__)

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


class ImportedWorktree(BaseModel):
    repo: str
    name: str
    path: str
    branch: str
    ticket: str | None = None


class SkippedWorktree(BaseModel):
    repo: str
    path: str
    reason: str


class DiscoverResponse(BaseModel):
    imported: list[ImportedWorktree]
    skipped: list[SkippedWorktree]


@router.post("/worktrees/discover", response_model=DiscoverResponse)
async def discover_worktrees() -> DiscoverResponse:
    """Iterate every configured repo and ingest the worktrees git
    already knows about. Per-repo failures appear in ``skipped[]``
    (e.g. ``repo path missing``) rather than aborting the request, so
    one broken repo doesn't block import for the others.
    """
    result = await asyncio.to_thread(discover_all_sync)
    return DiscoverResponse(**result)


@router.get("/worktree/{repo}/{name}", response_model=WorktreeDetail)
async def get_worktree(repo: str, name: str) -> WorktreeDetail:
    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}")
    return WorktreeDetail(row=row, log=svc.get_log(repo, name))


class PrUrlResponse(BaseModel):
    url: str


def _pr_url_from_row(row: WorktreeRow) -> str | None:
    if row.pr_number is None or not row.pr_repo:
        return None
    return f"https://github.com/{row.pr_repo}/pull/{row.pr_number}"


async def _gh_pr_view(cwd: Path) -> dict | None:
    """Shell `gh pr view --json ...` in the given worktree path.

    Returns the parsed JSON dict (with `number`, `url`, `headRepository`)
    if a PR exists; ``None`` if `gh` reports no PR for the current branch.
    Raises ``HTTPException`` for any other failure (gh missing, not
    authed, network down, repo not on GitHub).
    """
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "pr",
        "view",
        "--json",
        "number,url,headRepository,headRepositoryOwner",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    stdout_b, stderr_b = await proc.communicate()
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        if "no pull requests found" in stderr.lower() or "no pr found" in stderr.lower():
            return None
        if "executable file not found" in stderr.lower() or "command not found" in stderr.lower():
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "`gh` CLI not found on PATH. Install GitHub CLI to enable PR lookups.",
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"`gh pr view` failed: {stderr.strip() or 'unknown error'}",
        )

    try:
        return json.loads(stdout_b.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not parse `gh pr view` output: {e}",
        ) from e


@router.get("/worktree/{repo}/{name}/pr-url", response_model=PrUrlResponse)
async def get_pr_url(repo: str, name: str) -> PrUrlResponse:
    """Resolve the GitHub PR URL for a worktree's branch.

    Uses cached ``pr_number`` + ``pr_repo`` from SQLite when present.
    Otherwise shells ``gh pr view`` inside the worktree, caches the
    result, and returns the URL. 404 if no PR exists yet.
    """
    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}")

    cached = _pr_url_from_row(row)
    if cached is not None:
        return PrUrlResponse(url=cached)

    worktree_path = Path(row.path)
    if not worktree_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"worktree path missing on disk: {worktree_path}",
        )

    data = await _gh_pr_view(worktree_path)
    if data is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no open PR found for branch '{row.branch}'",
        )

    pr_number = data.get("number")
    url = data.get("url")
    head_repo = data.get("headRepository") or {}
    head_owner = data.get("headRepositoryOwner") or {}
    repo_name = head_repo.get("name")
    owner_login = head_owner.get("login")

    if not isinstance(pr_number, int) or not isinstance(url, str):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "`gh pr view` returned an unexpected payload shape",
        )

    pr_repo: str | None = None
    if isinstance(owner_login, str) and isinstance(repo_name, str):
        pr_repo = f"{owner_login}/{repo_name}"

    if pr_repo:
        await asyncio.to_thread(
            svc.update_worktree_pr_sync, repo, name, pr_number, pr_repo
        )

    return PrUrlResponse(url=url)


@router.post(
    "/worktree/{repo}/{name}/pr-state/refresh", response_model=PrStateSummary
)
async def refresh_pr_state(repo: str, name: str) -> PrStateSummary:
    """Force-refresh the cached PR state for this worktree by shelling
    `gh pr view` synchronously. Returns the fresh classified summary.
    Used by the popover's "Refresh now" button so the user doesn't
    have to wait for the next polling tick (~3 min)."""
    from app.services.pr_state import (
        GhUnavailable,
        fetch_pr_summary,
        upsert_pr_state_sync,
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

    try:
        summary = await fetch_pr_summary(worktree_path)
    except GhUnavailable as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "`gh` CLI not on PATH. Install GitHub CLI to enable PR state.",
        ) from e

    checked_at = await asyncio.to_thread(
        upsert_pr_state_sync, repo, name, summary
    )

    payload = summary.to_payload()
    payload["checked_at"] = checked_at
    return PrStateSummary.model_validate(payload)


class SpawnItermResponse(BaseModel):
    window_id: str
    claude_session_id: str
    shell_session_id: str
    # The Claude Code session UUID, discovered by polling
    # ~/.claude/projects/<encoded-cwd>/*.jsonl after spawn (plan §7).
    # null if discovery timed out within ~30s.
    claude_session_uuid: str | None = None
    # Path to the sidecar file written for the token-monitor (only if
    # session UUID was discovered).
    sidecar_path: str | None = None


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

    # Capture an mtime floor BEFORE we send `claude\n` to iTerm2 so the
    # discovery poll only matches the new jsonl, not any leftover from a
    # prior Claude session in the same cwd.
    mtime_floor = time.time()

    try:
        result: SpawnResult = await spawn_worktree_window(iterm.connection, worktree_path, frame)
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    # Persist the iterm_session row right now with no UUID — the
    # has_claude_session badge on the hub depends on this row existing,
    # and a fire-and-forget background task fills the UUID in later
    # once Claude has written its jsonl. That way the HTTP response
    # returns the instant the iTerm2 window is up, instead of blocking
    # the user-facing button for the full discovery timeout (up to
    # ~30s) when they close the window before Claude finished starting.
    await asyncio.to_thread(upsert_iterm_sessions_sync, repo, name, result, None)

    _spawn_post_discovery_task(
        repo=repo,
        name=name,
        ticket=row.ticket,
        pr_number=row.pr_number,
        pr_repo=row.pr_repo,
        worktree_path=worktree_path,
        mtime_floor=mtime_floor,
        window_id=result.window_id,
    )

    return SpawnItermResponse(
        window_id=result.window_id,
        claude_session_id=result.claude_session_id,
        shell_session_id=result.shell_session_id,
        # These are populated by the background task — clients that
        # care can read the iterm_session row a moment later. Inline
        # response fields stay for back-compat.
        claude_session_uuid=None,
        sidecar_path=None,
    )


# Strong refs to in-flight background tasks. asyncio.create_task only
# holds a weak ref to the returned Task; without this set, a discovery
# task could be GC'd mid-poll and silently vanish.
_post_spawn_tasks: set[asyncio.Task] = set()


def _spawn_post_discovery_task(
    *,
    repo: str,
    name: str,
    ticket: str | None,
    pr_number: int | None,
    pr_repo: str | None,
    worktree_path: Path,
    mtime_floor: float,
    window_id: str,
) -> None:
    task = asyncio.create_task(
        _post_spawn_discovery(
            repo=repo,
            name=name,
            ticket=ticket,
            pr_number=pr_number,
            pr_repo=pr_repo,
            worktree_path=worktree_path,
            mtime_floor=mtime_floor,
            window_id=window_id,
        )
    )
    _post_spawn_tasks.add(task)
    task.add_done_callback(_post_spawn_tasks.discard)


async def _post_spawn_discovery(
    *,
    repo: str,
    name: str,
    ticket: str | None,
    pr_number: int | None,
    pr_repo: str | None,
    worktree_path: Path,
    mtime_floor: float,
    window_id: str,
) -> None:
    """Poll for Claude's jsonl, write the token-monitor sidecar, and
    update the iterm_session row's ``claude_session_uuid``. Runs after
    the spawn-iterm HTTP response returns. Failures and timeouts only
    log — the window is already up, which is all the HTTP caller cared
    about.

    The UUID update is race-safe: it only writes if the row still
    points at ``window_id``, so a later spawn that took over the same
    worktree won't be clobbered by this task's late-arriving UUID.
    """
    try:
        claude_uuid = await discover_session_id(worktree_path, mtime_floor)
    except Exception as e:
        log.warning(
            "post-spawn session_id discovery failed for %s/%s: %s", repo, name, e
        )
        return

    if claude_uuid is None:
        log.info(
            "post-spawn session_id discovery timed out for %s/%s — no sidecar written",
            repo, name,
        )
        return

    try:
        sidecar = build_sidecar(
            session_id=claude_uuid,
            worktree=f"{repo}_{name}",
            ticket=ticket,
            pr_number=pr_number,
            pr_repo=pr_repo,
        )
        await asyncio.to_thread(write_sidecar_sync, claude_uuid, sidecar)
    except Exception as e:
        log.warning("post-spawn sidecar write failed for %s: %s", claude_uuid, e)
        # Fall through: still try to record the UUID on the DB row.

    try:
        rows = await asyncio.to_thread(
            set_iterm_session_uuid_sync, repo, name, window_id, claude_uuid
        )
        if rows == 0:
            log.info(
                "post-spawn UUID update for %s/%s skipped: row was overtaken by a newer spawn",
                repo, name,
            )
    except Exception as e:
        log.warning("post-spawn UUID DB update failed for %s/%s: %s", repo, name, e)


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
    """Run a slash command in this worktree's Claude session.

    ``req.skill_name`` must appear in ``config.workspace_skills`` —
    that list is the server-side allow-list (symmetric with how
    ``/api/skills/global`` enforces ``config.global_skills``). The
    frontend only renders buttons for in-config skills, so this is
    mainly defense against hand-rolled curl callers.

    If no Claude session exists yet, spawn one in the worktree path
    with the slash command as the initial prompt (``claude '/<skill>'``).
    That gives the user a one-click "fire the skill" affordance from
    the workspace page even when nothing is open yet — same trick the
    hub-level global-skill button uses.

    If a session exists, send the slash command via the existing
    send-text path (CR-terminated for Claude's TUI).
    """
    config = load_config()
    if not any(s.name == req.skill_name for s in config.workspace_skills):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown workspace skill: {req.skill_name!r}. Add it to "
            "`workspace_skills` in ~/.config/cdh/config.yaml.",
        )

    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API.",
        )

    claude_sid = await asyncio.to_thread(get_claude_session_id_sync, repo, name)
    if claude_sid is not None:
        return await _send_to_worktree_claude(
            request, repo, name, f"/{req.skill_name}", press_enter=True
        )

    # No Claude session yet — spawn one with the slash command as initial
    # prompt. Mirrors the spawn-iterm route except we pre-load the prompt
    # instead of just `claude`, and we don't bother with sidecar discovery
    # here (the skill runs at startup; the sidecar can be backfilled the
    # next time the user opens a window).
    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}"
        )

    worktree_path = Path(row.path)
    if not worktree_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"worktree path missing on disk: {worktree_path}",
        )

    frame = config.iterm2.default_window
    try:
        result = await spawn_worktree_window(
            iterm.connection,
            worktree_path,
            frame,
            initial_prompt=f"/{req.skill_name}",
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    await asyncio.to_thread(upsert_iterm_sessions_sync, repo, name, result, None)
    return SendResponse(sent=True)
