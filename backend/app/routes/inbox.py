"""Inbox HTTP endpoints.

- ``GET /api/inbox`` — read the latest cached inbox poll result.
- ``POST /api/inbox/{pr_repo}/{pr_number}/pull-down`` — fetch the
  PR's branch and create a local worktree for it.

The poll loop in :mod:`app.services.inbox_poll` runs every 60s and
writes to ``app.state.inbox``. The read endpoint just serializes that;
if the first poll hasn't completed yet, ``prs=[]`` / ``checked_at=null``
is returned and the frontend renders a quiet loading state.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.config.loader import load_config
from app.services import worktree as wt_svc
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.inbox_poll import InboxCache, InboxPr
from app.services.inbox_search import configured_repos_index, lookup_configured_repo
from app.services.iterm_spawn import spawn_global_claude_window

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["inbox"])


class InboxPrPayload(BaseModel):
    pr_repo: str
    pr_number: int
    title: str
    author_login: str
    head_ref: str
    base_ref: str
    is_draft: bool
    url: str
    updated_at: str
    ci_status: str
    source: str
    stack_top_pr_number: int | None = None
    stack_size: int
    stack_position: int
    repo_configured: bool


class InboxResponse(BaseModel):
    prs: list[InboxPrPayload]
    checked_at: str | None = None


def _to_payload(pr: InboxPr) -> InboxPrPayload:
    return InboxPrPayload(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=pr.title,
        author_login=pr.author_login,
        head_ref=pr.head_ref,
        base_ref=pr.base_ref,
        is_draft=pr.is_draft,
        url=pr.url,
        updated_at=pr.updated_at,
        ci_status=pr.ci_status,
        source=pr.source,
        stack_top_pr_number=pr.stack_top_pr_number,
        stack_size=pr.stack_size,
        stack_position=pr.stack_position,
        repo_configured=pr.repo_configured,
    )


@router.get("/inbox", response_model=InboxResponse)
async def get_inbox(request: Request) -> InboxResponse:
    cache: InboxCache | None = getattr(request.app.state, "inbox", None)
    if cache is None:
        return InboxResponse(prs=[], checked_at=None)
    return InboxResponse(
        prs=[_to_payload(p) for p in cache.prs],
        checked_at=cache.checked_at,
    )


# --- pull-down ----------------------------------------------------------


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
    """``git fetch origin pull/<n>/head:<local_branch>`` inside the
    configured repo's checkout. Raises HTTPException 502 on failure."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "origin",
        f"pull/{pr_number}/head:{local_branch}",
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
    pr_repo: str, pr_number: int, *, cache: InboxCache | None
) -> PullDownResponse:
    """Pure pull-down logic, independent of any HTTP request object.

    Raises :class:`HTTPException` for the same conditions as the
    request handler so the configure-and-pull-down follow-up can log
    a structured failure. Callers in a background context catch broad
    Exception around this; the inbox-route caller lets it bubble.
    """
    if cache is None or not any(
        p.pr_repo == pr_repo and p.pr_number == pr_number for p in cache.prs
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} not in inbox cache",
        )

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
    # the inbox cache is up to 60s stale and the fork bit can flip via
    # a PR re-target).
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

    # Set pr_number + pr_repo on the new worktree row so the inbox dedup
    # filter applies on the next poll (and so the PR-URL button on the
    # hub can short-circuit without re-shelling `gh pr view`).
    await asyncio.to_thread(
        wt_svc.update_worktree_pr_sync,
        repo.name,
        worktree.name,
        pr_number,
        pr_repo,
    )

    return PullDownResponse(repo=repo.name, name=worktree.name)


@router.post(
    "/inbox/{pr_repo:path}/{pr_number}/pull-down",
    response_model=PullDownResponse,
)
async def pull_down(
    pr_repo: str, pr_number: int, request: Request
) -> PullDownResponse:
    """Fetch the PR's branch into the configured local repo (handling
    same-repo and fork PRs) and create a worktree for it.

    Refuses when:

    - The PR isn't in the most recent inbox poll (404). Prevents pulling
      down something the user wasn't actually looking at.
    - The PR's repo doesn't match any configured ``RepoConfig`` (400).
      The frontend uses ``repo_configured`` to disable the button, but
      the backend re-checks since config could have changed between
      the poll and the click.
    """
    cache: InboxCache | None = getattr(request.app.state, "inbox", None)
    return await _perform_pull_down(pr_repo, pr_number, cache=cache)


# --- configure-and-pull-down --------------------------------------------


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

    - The PR isn't in the most recent inbox poll (404).
    - The repo IS already configured (409) — the regular pull-down
      endpoint covers that case.
    - iTerm2 isn't connected (503).
    """
    cache: InboxCache | None = getattr(request.app.state, "inbox", None)
    if cache is None or not any(
        p.pr_repo == pr_repo and p.pr_number == pr_number for p in cache.prs
    ):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} not in inbox cache",
        )

    config = load_config()
    if lookup_configured_repo(pr_repo, configured_repos_index(config.repos)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"repo {pr_repo} is already configured — use the regular pull-down",
        )

    iterm = getattr(request.app.state, "iterm", None)
    if iterm is None or iterm.connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "iTerm2 not connected. Check Preferences → Magic → Enable Python "
            "API and approve the first-connection auth dialog.",
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

    try:
        await spawn_global_claude_window(
            iterm.connection, dev_root, config.iterm2.default_window, prompt
        )
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"iTerm2 spawn failed: {e}"
        ) from e

    return ConfigureAndPullDownResponse(session_id=session_id)
