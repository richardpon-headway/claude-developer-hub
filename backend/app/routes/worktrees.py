"""REST endpoints for the worktree CRUD slice + the iTerm2 spawn endpoint.

Delete / retry-from-step / force-remove come later when the workspace
page needs them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.models.worktree import PrStateSummary, WorktreeRow
from app.services import worktree as svc
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.iterm_send import (
    SendGateError,
    SessionNotFoundError,
    send_to_session,
)
from app.services.iterm_spawn import (
    SpawnResult,
    delete_iterm_sessions_sync,
    focus_iterm_window,
    get_claude_session_id_sync,
    get_claude_window_and_session_sync,
    set_iterm_session_uuid_sync,
    spawn_two_tab_window,
    upsert_iterm_sessions_sync,
)
from app.services.sidecar import (
    build_sidecar,
    discover_session_id,
    write_sidecar_sync,
)
from app.services.worktree_import import sync_all_sync

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


@router.post("/worktree/{repo}/{name}/recreate", response_model=WorktreeRow)
async def recreate_worktree(repo: str, name: str) -> WorktreeRow:
    """Drop a stale worktree row + re-run the full create flow against
    the same branch. Used by the "Recreate workspace" button on rows
    whose on-disk path was deleted outside CDH.

    Constrained to ``status='stale'`` rows only — a ready/setting_up/
    failed row has on-disk state we shouldn't blow away without the
    user thinking about it.
    """
    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}"
        )
    if row.status != "stale":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"recreate only applies to stale worktrees (this one is "
            f"'{row.status}'). Investigate or delete it manually first.",
        )

    # Drop the row (CASCADEs iterm_session + pr_state) before re-running
    # create_worktree, which inserts a fresh row from scratch.
    await asyncio.to_thread(svc.delete_worktree_sync, repo, name)

    # If the user did `rm -rf` on the directory without also running
    # `git worktree prune`, git still tracks the (now-broken)
    # worktree and a fresh `git worktree add <same path>` would fail
    # with "already exists" from git. Run prune here so recreate
    # works whether or not the user cleaned up git's tracking.
    config = load_config()
    repo_cfg = next((r for r in config.repos if r.name == repo), None)
    if repo_cfg is not None:
        repo_path = Path(str(repo_cfg.path)).expanduser()
        if repo_path.is_dir():
            prune = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_path), "worktree", "prune",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await prune.wait()

    try:
        return await svc.create_worktree(repo, row.branch)
    except svc.WorktreeCreationError as e:
        msg = str(e)
        code = (
            status.HTTP_409_CONFLICT
            if "already exists" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, msg) from e


class OpenCursorRequest(BaseModel):
    file: str | None = Field(
        default=None,
        description=(
            "Optional path relative to the worktree root. When set, "
            "opens that specific file in Cursor instead of the "
            "worktree folder."
        ),
    )


class OpenCursorResponse(BaseModel):
    opened: bool


@router.post(
    "/worktree/{repo}/{name}/open-cursor", response_model=OpenCursorResponse
)
async def open_in_cursor(
    repo: str,
    name: str,
    req: OpenCursorRequest | None = None,
) -> OpenCursorResponse:
    """Shell `cursor <target>` to open the worktree (folder by default,
    or a specific file when ``req.file`` is set) in Cursor. No
    pre-probe of the `cursor` CLI — we detect the missing-binary case
    from subprocess stderr and surface it as 503.
    """
    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}"
        )

    wt_path = Path(row.path)
    if not wt_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"worktree path missing on disk: {wt_path}",
        )

    target = wt_path
    if req is not None and req.file:
        # Resolve + verify the result stays under the worktree root.
        # Catches absolute paths, parent-traversal, and symlinks
        # pointing outside the tree.
        candidate = (wt_path / req.file).resolve()
        try:
            candidate.relative_to(wt_path.resolve())
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"file must live under the worktree root: {req.file}",
            ) from e
        if not candidate.exists():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"file does not exist: {req.file}",
            )
        target = candidate

    try:
        proc = await asyncio.create_subprocess_exec(
            "cursor",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        # `cursor` not on PATH at all — Python raises before exec.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Cursor CLI not on PATH. Install from cursor.com, then run "
            "Cmd+Shift+P → 'Shell Command: Install \"cursor\" command'.",
        ) from e

    _, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        lower = stderr.lower()
        if (
            "executable file not found" in lower
            or "command not found" in lower
        ):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Cursor CLI not on PATH. Install from cursor.com, then run "
                "Cmd+Shift+P → 'Shell Command: Install \"cursor\" command'.",
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"cursor exited {proc.returncode}: {stderr[:200]}",
        )

    return OpenCursorResponse(opened=True)


class PrFile(BaseModel):
    path: str
    additions: int
    deletions: int
    github_diff_anchor: str  # sha256(path).hexdigest()


class PrFilesResponse(BaseModel):
    files: list[PrFile]


@router.get(
    "/worktree/{repo}/{name}/pr-files", response_model=PrFilesResponse
)
async def get_pr_files(repo: str, name: str) -> PrFilesResponse:
    """Return the files changed on the PR associated with this
    worktree. Empty list if the worktree has no PR, or if ``gh pr
    view`` reports no PR is open for the branch.

    Prefers the ``pr_number`` + ``pr_repo`` cached on the worktree
    row (populated lazily by the pr-state poller). Falls back to
    ``gh pr view --json files`` in the worktree path when those
    aren't set, so freshly-created rows still work pre-poll.
    """
    import hashlib

    row = await asyncio.to_thread(svc.get_worktree_sync, repo, name)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"worktree not found: {repo}/{name}"
        )

    wt_path = Path(row.path)
    if not wt_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"worktree path missing on disk: {wt_path}",
        )

    try:
        if row.pr_number is not None and row.pr_repo is not None:
            payload = await run_gh_json(
                [
                    "pr",
                    "view",
                    str(row.pr_number),
                    "-R",
                    row.pr_repo,
                    "--json",
                    "files",
                ],
                swallow_errors=True,
            )
        else:
            payload = await run_gh_json(
                ["pr", "view", "--json", "files"],
                cwd=wt_path,
                swallow_errors=True,
            )
    except GhNotFound as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "gh CLI not on PATH; cannot list PR files.",
        ) from e

    if payload is None or not isinstance(payload, dict):
        return PrFilesResponse(files=[])
    raw = payload.get("files") or []
    files: list[PrFile] = []
    for entry in raw:
        path_str = str(entry.get("path") or "")
        if not path_str:
            continue
        files.append(
            PrFile(
                path=path_str,
                additions=int(entry.get("additions") or 0),
                deletions=int(entry.get("deletions") or 0),
                github_diff_anchor=hashlib.sha256(path_str.encode()).hexdigest(),
            )
        )
    return PrFilesResponse(files=files)


@router.get("/worktrees", response_model=list[WorktreeRow])
async def list_worktrees() -> list[WorktreeRow]:
    return await asyncio.to_thread(svc.list_worktrees_sync)


class ImportedWorktree(BaseModel):
    repo: str
    name: str
    path: str
    branch: str
    ticket: str | None = None


class RemovedWorktree(BaseModel):
    repo: str
    name: str
    path: str
    reason: str


class SkippedWorktree(BaseModel):
    repo: str
    path: str
    reason: str


class SyncResponse(BaseModel):
    imported: list[ImportedWorktree]
    removed: list[RemovedWorktree]
    skipped: list[SkippedWorktree]


@router.post("/worktrees/sync", response_model=SyncResponse)
async def sync_worktrees() -> SyncResponse:
    """Reconcile every configured repo's worktree list with the DB:
    insert rows for new worktrees git knows about, drop rows whose
    path is no longer in ``git worktree list``. Per-repo failures
    appear in ``skipped[]`` (e.g. ``repo path missing``) rather than
    aborting the request, so one broken repo doesn't block reconcile
    for the others.
    """
    result = await asyncio.to_thread(sync_all_sync)
    return SyncResponse(**result)


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
    """Shell ``gh pr view --json …`` in the given worktree path.

    Returns the parsed JSON dict (with ``number``, ``url``,
    ``headRepository``) if a PR exists; ``None`` if ``gh`` reports no
    PR for the current branch. Raises ``HTTPException(502)`` for any
    other failure (``gh`` missing, network down, repo not on GitHub).
    """
    try:
        data = await run_gh_json(
            ["pr", "view", "--json", "number,url,headRepository,headRepositoryOwner"],
            cwd=cwd,
            swallow_errors=False,
        )
    except GhNotFound as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "`gh` CLI not found on PATH. Install GitHub CLI to enable PR lookups.",
        ) from e
    except GhFailed as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    # run_gh_json returns dict | list | None; gh pr view's JSON is a dict
    # (or None for the "no PR" case). Narrow for the caller.
    return data if isinstance(data, dict) else None


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
    except GhNotFound as e:
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


class FocusItermResponse(BaseModel):
    focused: bool


@router.post("/worktree/{repo}/{name}/focus-iterm", response_model=FocusItermResponse)
async def focus_iterm(repo: str, name: str, request: Request) -> FocusItermResponse:
    """Bring this worktree's already-open iTerm2 window to the front.

    Differs from ``spawn-iterm``: this never creates a new window. It
    only activates an existing one. The frontend uses this for the
    ``claude ●`` pill so the user can return to a running session
    without spawning a duplicate window.

    Returns 503 if iTerm2 isn't connected, 404 if no claude session
    is tracked for this worktree, and 404 (with the stale row pruned)
    if the tracked window no longer exists in iTerm2.
    """
    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python API.",
        )

    row = await asyncio.to_thread(
        get_claude_window_and_session_sync, repo, name
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no tracked Claude session for {repo}/{name}",
        )

    window_id, session_id = row
    try:
        ok = await focus_iterm_window(iterm.connection, window_id, session_id)
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 focus failed: {e}"
        ) from e

    if not ok:
        # Window is gone — the user closed it manually, or iTerm2
        # restarted. Prune the stale row so the claude ● pill drops
        # on the next worktrees-poll.
        await asyncio.to_thread(delete_iterm_sessions_sync, repo, name)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "tracked iTerm2 window is gone; session row pruned. "
            "Click iTerm2 to spawn a fresh window.",
        )

    return FocusItermResponse(focused=True)


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
        result: SpawnResult = await spawn_two_tab_window(iterm.connection, worktree_path, frame)
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


async def _spawn_with_prompt(
    request: Request, repo: str, name: str, initial_prompt: str
) -> SendResponse:
    """Spawn a fresh iTerm2 window in the worktree path with
    ``claude '<initial_prompt>'`` as the first message. Used as the
    fallback path for both run-skill and send-text when no live Claude
    session exists for the worktree. The window's iterm_session row is
    upserted so future sends use the existing-session path."""
    config = load_config()
    iterm = request.app.state.iterm  # caller already checked iterm.connection
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
        result = await spawn_two_tab_window(
            iterm.connection,
            worktree_path,
            frame,
            initial_prompt=initial_prompt,
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    await asyncio.to_thread(upsert_iterm_sessions_sync, repo, name, result, None)
    return SendResponse(sent=True)


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
        # No tracked session — spawn one with the text as the initial
        # prompt instead of refusing. Mirrors the auto-spawn-on-miss
        # behavior the skill buttons already provide. press_enter is
        # implicit: claude's positional-arg prompt fires at startup.
        return await _spawn_with_prompt(request, repo, name, text)

    try:
        await send_to_session(iterm.connection, claude_sid, text, press_enter=press_enter)
    except SessionNotFoundError:
        # DB row pointed at a window that no longer exists (user closed
        # it manually, iTerm2 restarted, etc). Prune the stale row and
        # fall through to spawning a fresh one with this text as the
        # initial prompt — same UX as if no row had ever existed.
        log.info(
            "send-text found stale iterm_session for %s/%s; pruning and respawning",
            repo, name,
        )
        await asyncio.to_thread(delete_iterm_sessions_sync, repo, name)
        return await _spawn_with_prompt(request, repo, name, text)
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
    ``/api/skills/global`` enforces ``config.global_skills``).

    Delegates to the shared send-text path which handles three cases:

    - Live Claude session: send ``/<skill>\\r`` via iTerm2 (CR submits).
    - DB row exists but iTerm2 lost the session (stale row from a
      manually-closed window or an iTerm2 restart): prune the row and
      spawn a fresh window with ``claude '/<skill>'`` as initial prompt.
    - No DB row at all: spawn the same way.
    """
    config = load_config()
    if not any(s.name == req.skill_name for s in config.workspace_skills):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown workspace skill: {req.skill_name!r}. Add it to "
            "`workspace_skills` in ~/.config/cdh/config.yaml.",
        )

    return await _send_to_worktree_claude(
        request, repo, name, f"/{req.skill_name}", press_enter=True
    )
