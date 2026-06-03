"""Fetch a PR's branch into a configured repo and create a worktree.

Surface-agnostic engine shared by the bookmark and authored-PR
pull-down routes. The HTTP routes own their own "PR exists in this
surface" guards; this module just wires up the ``gh`` fetch + worktree
creation once the caller has decided the pull-down is allowed.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi import HTTPException, status
from pydantic import BaseModel

from app.config.loader import load_config
from app.models.pr import PrRow
from app.services import pr_db
from app.services import worktree as wt_svc
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.repos_index import (
    configured_repos_index,
    lookup_configured_repo,
)


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


async def perform_pull_down(
    pr_repo: str, pr_number: int, *, author_login: str | None = None
) -> PullDownResponse:
    """Pure pull-down logic, independent of any HTTP request object.

    The caller is responsible for any "PR exists in some surface"
    guard appropriate to the entry point (bookmark row, authored-PR
    list). This function just wires up the gh fetch + worktree
    creation.

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
    # the bookmark/authored row's title is enriched but we never
    # captured headRefName, so we need a real `gh pr view` here).
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

    # Set pr_number + pr_repo on the new worktree row so the surface's
    # dedup filter (has_worktree=False) excludes this PR.
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
