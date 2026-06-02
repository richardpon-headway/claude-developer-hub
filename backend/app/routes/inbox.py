"""Inbox HTTP endpoints (persistent-inbox redesign — see plan-48).

- ``GET /api/inbox`` — list rows from the unified ``pr`` table where
  ``is_inbox=1`` and the row hasn't been archived / bookmarked /
  pulled-down into a worktree (read-time surface precedence).
- ``POST /api/inbox/refresh`` — force an immediate poll tick.
- ``POST /api/inbox/{pr_repo}/{pr_number}/archive`` — sticky-dismiss
  a row (sets ``is_archived=1`` + clears ``is_inbox``).
- ``PUT /api/inbox/{pr_repo}/{pr_number}/notes`` — overwrite the
  ``pr.notes`` column. Empty string clears the note.
- ``POST /api/inbox/{pr_repo}/{pr_number}/pull-down`` — fetch the PR's
  branch and create a local worktree.
- ``POST /api/inbox/{pr_repo}/{pr_number}/configure-and-pull-down`` —
  spawn Claude to onboard the upstream repo, with a follow-up that
  triggers pull-down once onboarding completes.

The poll loop in :mod:`app.services.inbox_poll` runs every 60s and
upserts into ``pr`` via :mod:`pr_db`. Read endpoints just query
SQLite — no in-memory state.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.models.pr import PrCiStatus, PrRow
from app.models.worktree import now_iso
from app.services import inbox_poll, pr_db, terminal
from app.services import worktree as wt_svc
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.inbox_search import (
    ReposIndex,
    configured_repos_index,
    lookup_configured_repo,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["inbox"])


# Soft upper bound for inbox notes. Matches worktree notes (10K) — a
# runaway paste shouldn't blow up the DB, but normal usage is a few
# short lines.
_NOTES_MAX_LENGTH = 10_000


# ---------------------------------------------------------------------
# Response payload
# ---------------------------------------------------------------------


class InboxPr(BaseModel):
    """Wire shape for one inbox row."""

    pr_repo: str
    pr_number: int
    title: str
    author_login: str
    url: str
    is_draft: bool
    ci_status: PrCiStatus
    sources: list[str]
    notes: str | None = None
    ticket: str | None = None
    pr_updated_at: str
    added_at: str
    last_seen_at: str
    # Derived at serialize time from the live config so the frontend
    # can disable Pull-down when the upstream repo isn't onboarded.
    repo_configured: bool


class InboxResponse(BaseModel):
    prs: list[InboxPr]


def _payload_from_row(pr: PrRow, *, repos_index: ReposIndex) -> InboxPr:
    return InboxPr(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=pr.title or "",
        author_login=pr.author_login or "",
        url=pr.url or "",
        is_draft=pr.is_draft,
        ci_status=pr.ci_status or "none",
        sources=list(pr.inbox_sources),
        notes=pr.notes,
        ticket=pr.ticket,
        pr_updated_at=pr.pr_updated_at or "",
        added_at=pr.inbox_added_at or "",
        last_seen_at=pr.last_seen_at or "",
        repo_configured=lookup_configured_repo(pr.pr_repo, repos_index) is not None,
    )


# ---------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------


@router.get("/inbox", response_model=InboxResponse)
async def get_inbox() -> InboxResponse:
    rows = await asyncio.to_thread(
        pr_db.list_pr_sync,
        is_inbox=True,
        is_archived=False,
        is_bookmarked=False,
        has_worktree=False,
        order_by="pr.pr_updated_at DESC",
    )
    repos = (await asyncio.to_thread(load_config)).repos
    idx = configured_repos_index(repos)
    return InboxResponse(prs=[_payload_from_row(r, repos_index=idx) for r in rows])


@router.post("/inbox/refresh", response_model=InboxResponse)
async def refresh_inbox(request: Request) -> InboxResponse:
    """Force an immediate inbox poll tick + return the post-tick list.

    Used by the hub's Sync button so the user doesn't have to wait up
    to 60s for the next background refresh. ``gh`` failures inside the
    tick are swallowed by the same handler as the background path, so
    this endpoint is safe to call even when ``gh`` is misbehaving.
    """
    await inbox_poll._tick(request.app.state)
    return await get_inbox()


# ---------------------------------------------------------------------
# Archive (sticky dismissal)
# ---------------------------------------------------------------------


@router.post(
    "/inbox/{pr_repo:path}/{pr_number}/archive",
    response_model=InboxPr,
)
async def archive_inbox(pr_repo: str, pr_number: int) -> InboxPr:
    """Mark the row as user-dismissed: clears ``is_inbox`` and sets
    ``is_archived=1`` (preserving the original ``archived_at`` via
    COALESCE — see ``set_archived_flag_sync``).

    Refuses (404) when no matching inbox row exists, including a
    repeat archive of a row already dismissed (since the inbox flag
    is gone). Guards against archiving a row the poll already
    auto-removed (close/merge race).
    """
    row = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if row is None or not row.is_inbox:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} not in inbox",
        )
    # Set archived first so the row stays addressable through inbox_keys
    # callers until the inbox flag drops. (Order matters when GC paths
    # check origin flags between the two writes.)
    await asyncio.to_thread(
        pr_db.set_archived_flag_sync,
        pr_repo, pr_number, True, archived_at=now_iso(),
    )
    await asyncio.to_thread(
        pr_db.set_inbox_flag_sync, pr_repo, pr_number, False
    )
    repos = (await asyncio.to_thread(load_config)).repos
    return _payload_from_row(row, repos_index=configured_repos_index(repos))


# ---------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------


class UpdateNotesRequest(BaseModel):
    notes: str = Field(..., max_length=_NOTES_MAX_LENGTH)


class UpdateNotesResponse(BaseModel):
    notes: str


@router.put(
    "/inbox/{pr_repo:path}/{pr_number}/notes",
    response_model=UpdateNotesResponse,
)
async def update_notes(
    pr_repo: str, pr_number: int, req: UpdateNotesRequest
) -> UpdateNotesResponse:
    """Overwrite the row's notes. Empty string is valid (clears).
    Refuses (404) when no matching inbox row exists."""
    existing = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if existing is None or not existing.is_inbox:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} not in inbox",
        )
    await asyncio.to_thread(
        pr_db.update_notes_sync, pr_repo, pr_number, req.notes
    )
    return UpdateNotesResponse(notes=req.notes)


# ---------------------------------------------------------------------
# Pull-down
# ---------------------------------------------------------------------


class PullDownResponse(BaseModel):
    """The new worktree's identifiers, so the frontend can route to its
    workspace page (or invalidate the worktrees query for the hub)."""

    repo: str
    name: str


# Git branch names allow most printable characters; we sanitize only
# what would break a shell argument or filesystem path. The PR ref's
# head_ref is GitHub-provided, so the surface area is small.
_BRANCH_SAFE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _local_branch_for_fork_pr(pr_number: int, head_ref: str) -> str:
    """For fork PRs we fetch ``refs/pull/<n>/head`` into a local branch
    whose name embeds the PR number. Same-repo PRs just check out the
    upstream branch by its existing name. The ``cdh-pr-`` prefix makes
    these branches easy to identify and prune later."""
    return f"cdh-pr-{pr_number}-{head_ref}"


async def _fetch_pr_ref(
    repo_path: Path, pr_number: int, local_branch: str
) -> None:
    """``git fetch origin +pull/<n>/head:<local_branch>`` inside the
    configured repo's checkout. The leading ``+`` makes the fetch
    force-update the local branch, so a stale ref from a prior killed
    pull-down (or a force-pushed PR HEAD) gets overwritten with the
    current GitHub PR HEAD — pull-down stays idempotent under retry.
    Raises HTTPException 502 on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "origin",
        f"+pull/{pr_number}/head:{local_branch}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(repo_path),
    )
    _, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_b.decode("utf-8", errors="replace")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"git fetch pull/{pr_number}/head failed: "
            f"{stderr.strip() or 'unknown error'}",
        )


async def _perform_pull_down(
    pr_repo: str, pr_number: int, *, author_login: str | None = None
) -> PullDownResponse:
    """Pure pull-down logic, independent of any HTTP request object.

    The caller is responsible for any "PR exists in some surface"
    guard appropriate to the entry point (inbox row, authored-PR list,
    onboard-complete callback). This function just wires up the gh
    fetch + worktree creation.

    ``author_login`` is written to ``pr.author_login`` (the unified
    pr table — worktree.pr_author_login was dropped by migration 013
    and the field is projected via LEFT JOIN at read time). ``None``
    is acceptable; the pr_state poll fills the column on its next
    tick from gh's fresh payload.
    """
    config = load_config()
    repo = lookup_configured_repo(pr_repo, configured_repos_index(config.repos))
    if repo is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"repo not configured: {pr_repo}"
        )

    repo_path = Path(str(repo.path)).expanduser()
    if not repo_path.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"configured repo path missing on disk: {repo_path}",
        )

    # Resolve the PR's head branch + fork-ness from gh (authoritative —
    # the inbox row's title is enriched but we never captured headRefName
    # from `gh search prs`, so we need a real `gh pr view` here).
    try:
        data = await run_gh_json(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                pr_repo,
                "--json",
                "headRefName,isCrossRepository",
            ],
            swallow_errors=False,
        )
    except GhNotFound as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "`gh` CLI not found on PATH. Install GitHub CLI to enable pull-down.",
        ) from e
    except GhFailed as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    if not isinstance(data, dict):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"`gh pr view {pr_number}` returned no payload — does this PR exist?",
        )

    head_ref = data.get("headRefName")
    if not isinstance(head_ref, str) or not _BRANCH_SAFE.match(head_ref):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"`gh pr view` returned an unexpected headRefName: {head_ref!r}",
        )
    is_fork = bool(data.get("isCrossRepository"))

    if is_fork:
        branch_to_check_out = _local_branch_for_fork_pr(pr_number, head_ref)
        await _fetch_pr_ref(repo_path, pr_number, branch_to_check_out)
    else:
        # create_worktree's built-in `git fetch origin --prune` covers
        # the same-repo case; the verify-remote step accepts
        # origin/<head_ref> so we don't need a pre-fetch.
        branch_to_check_out = head_ref

    try:
        worktree = await wt_svc.create_worktree(repo.name, branch_to_check_out)
    except wt_svc.WorktreeCreationError as e:
        msg = str(e)
        code = (
            status.HTTP_409_CONFLICT
            if "already exists" in msg
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, msg) from e

    # Set pr_number + pr_repo on the new worktree row. The dedup
    # filter in the inbox list (has_worktree=False) will then exclude
    # this PR.
    await asyncio.to_thread(
        wt_svc.update_worktree_pr_sync,
        repo.name,
        worktree.name,
        pr_number,
        pr_repo,
    )
    # Write the author onto the unified pr row (the worktree projects
    # it via JOIN). Captured at pull-down time from the originating
    # surface's row so the REVIEWING-tier split has a value before
    # the next pr_state poll tick.
    if author_login is not None:
        await asyncio.to_thread(
            pr_db.upsert_pr_sync,
            PrRow(
                pr_repo=pr_repo,
                pr_number=pr_number,
                author_login=author_login,
            ),
        )

    return PullDownResponse(repo=repo.name, name=worktree.name)


@router.post(
    "/inbox/{pr_repo:path}/{pr_number}/pull-down",
    response_model=PullDownResponse,
)
async def pull_down(pr_repo: str, pr_number: int) -> PullDownResponse:
    """Fetch the PR's branch into the configured local repo (handling
    same-repo and fork PRs) and create a worktree for it.

    Refuses when:

    - The PR isn't in the persisted inbox (404). Prevents pulling
      down random PRs by URL guessing.
    - The PR's repo doesn't match any configured ``RepoConfig`` (400).
      The frontend uses ``repo_configured`` to disable the button, but
      the backend re-checks since config could have changed between
      the poll and the click.
    """
    row = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if row is None or not row.is_inbox:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} not in inbox",
        )
    return await _perform_pull_down(
        pr_repo, pr_number, author_login=row.author_login
    )


# ---------------------------------------------------------------------
# Configure-and-pull-down (onboard the upstream repo, then pull down)
# ---------------------------------------------------------------------


class ConfigureAndPullDownResponse(BaseModel):
    """Returned immediately after spawning the Claude session. The
    worktree creation itself happens asynchronously once Claude POSTs
    its proposed_entry back to /api/repos/onboard/complete."""

    session_id: str


@router.post(
    "/inbox/{pr_repo:path}/{pr_number}/configure-and-pull-down",
    response_model=ConfigureAndPullDownResponse,
)
async def configure_and_pull_down(
    pr_repo: str, pr_number: int, request: Request
) -> ConfigureAndPullDownResponse:
    """Spawn Claude at ``config.development_root`` with a clone-and-
    inspect prompt for ``pr_repo``. When Claude POSTs the proposed
    config entry back, ``onboard_complete`` saves it and auto-fires the
    inbox pull-down for ``pr_number`` — no second click needed.

    Refuses when:

    - The PR isn't in the persisted inbox (404).
    - The repo IS already configured (409) — the regular pull-down
      endpoint covers that case.
    - iTerm2 isn't connected (503).
    """
    row = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if row is None or not row.is_inbox:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} not in inbox",
        )

    config = load_config()
    if lookup_configured_repo(pr_repo, configured_repos_index(config.repos)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"repo {pr_repo} is already configured — use the regular pull-down",
        )

    dev_root = Path(str(config.development_root)).expanduser()
    if not dev_root.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"development_root does not exist on disk: {dev_root}",
        )

    # pr_repo is `owner/name`. Clone target is `<development_root>/<name>`.
    # We pass this to Claude via the prompt; Claude is responsible for
    # checking "directory exists & is a git repo? use it; else clone".
    target = dev_root / pr_repo.split("/", 1)[1]

    # Delayed import to dodge a circular dependency between this module
    # and routes/repos.py.
    from app.routes.repos import mint_onboard_session

    session_id, inspection_prompt = await mint_onboard_session(
        target,
        follow_up={
            "kind": "pull_down",
            "pr_repo": pr_repo,
            "pr_number": pr_number,
        },
    )

    prompt = (
        f"First: ensure a local clone of `https://github.com/{pr_repo}` "
        f"exists at `{target}`. If `{target}` is already a git repo, "
        "use it as-is (it may be an existing clone CDH didn't know "
        "about); otherwise run `gh repo clone "
        f"{pr_repo} {target}` (or `git clone` if `gh` isn't available).\n\n"
        + inspection_prompt
    )

    await terminal.spawn_one_tab_claude(request, dev_root, prompt)
    return ConfigureAndPullDownResponse(session_id=session_id)
