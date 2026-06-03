"""Bookmark HTTP endpoints.

Bookmarks are manually-added PR watches: paste a GitHub PR URL into
the hub, the backend fetches the PR's metadata via ``gh pr view`` and
flips ``pr.is_bookmarked=1`` on the unified pr row. The lifecycle is
symmetric (explicit add, explicit remove). Close / merge never
auto-removes a bookmark — the row keeps rendering with a ``closed`` /
``merged`` chip until the user unbookmarks it.

A PR can only be bookmarked if its repo is already configured (in the
REPOS list). Bookmarking a PR from an unconfigured repo is refused
with 400 pointing the user at "Add a repo" — there's no point tracking
a PR you can't pull down.

- ``GET /api/bookmarks`` — list rows, newest-bookmarked first.
- ``POST /api/bookmarks`` — body ``{ url: string }``. Parses the URL,
  fetches via ``gh pr view``, upserts. Refuses 400 when the repo isn't
  configured. Idempotent-by-error: a second POST for the same PR
  returns 409 (so the user knows their existing bookmark wasn't
  overwritten).
- ``DELETE /api/bookmarks/{pr_repo}/{pr_number}`` — unbookmark.
- ``PUT /api/bookmarks/{pr_repo}/{pr_number}/notes`` — overwrite notes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.config.loader import load_config
from app.models.pr import PrRow, PrState
from app.models.worktree import now_iso
from app.services import pr_db
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.pr_search import extract_ticket
from app.services.pull_down import PullDownResponse, perform_pull_down
from app.services.repos_index import (
    configured_repos_index,
    is_repo_configured,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["bookmarks"])


# Soft cap on bookmark notes. Mirrors the worktree notes limit.
_NOTES_MAX_LENGTH = 10_000


_PR_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$"
)


# ---------------------------------------------------------------------
# Response payload
# ---------------------------------------------------------------------


class BookmarkPr(BaseModel):
    """Wire shape for one bookmark row."""

    pr_repo: str
    pr_number: int
    title: str
    author_login: str
    url: str
    state: PrState
    notes: str | None = None
    ticket: str | None = None
    bookmarked_at: str
    last_refreshed_at: str


def _payload_from_row(pr: PrRow) -> BookmarkPr:
    # last_refreshed_at fallback: pr_state.checked_at (set by the
    # enrichment poll) → pr.last_refreshed_at (set by the gh-pr-view
    # bookmark write) → bookmarked_at (always present on a bookmarked
    # row). Bookmarked_at is the floor because every bookmarked row
    # has it.
    checked_at = pr.pr_state.checked_at if pr.pr_state else None
    last_refreshed_at = (
        checked_at or pr.last_refreshed_at or pr.bookmarked_at or ""
    )
    return BookmarkPr(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=pr.title or "",
        author_login=pr.author_login or "",
        url=pr.url or "",
        state=pr.state or "open",
        notes=pr.notes,
        ticket=pr.ticket,
        bookmarked_at=pr.bookmarked_at or "",
        last_refreshed_at=last_refreshed_at,
    )


# ---------------------------------------------------------------------
# Add (paste URL)
# ---------------------------------------------------------------------


class AddBookmarkRequest(BaseModel):
    url: str = Field(..., min_length=1)


def _parse_pr_url(url: str) -> tuple[str, int]:
    """Parse a github.com PR URL into ``(owner/name, pr_number)``.

    Accepts the canonical form (``https://github.com/owner/repo/pull/123``)
    and tolerates trailing path segments (``/files``, ``/commits``, etc.)
    and query / hash fragments. Raises HTTPException 400 on any
    non-matching input.
    """
    m = _PR_URL_RE.match(url.strip())
    if m is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "expected a github.com pull-request URL, "
            "e.g. https://github.com/owner/repo/pull/123",
        )
    owner, name, n = m.group(1), m.group(2), m.group(3)
    return f"{owner}/{name}", int(n)


def _normalize_state(value: object) -> PrState:
    """Coerce ``gh pr view --json state`` to the PrState enum.

    GitHub returns ``OPEN`` / ``CLOSED`` / ``MERGED``. Anything else
    defaults to ``closed`` (defensive — a future GitHub state shouldn't
    crash the bookmark add)."""
    s = str(value or "").lower()
    if s in ("open", "closed", "merged"):
        return s  # type: ignore[return-value]
    return "closed"


@router.post(
    "/bookmarks",
    response_model=BookmarkPr,
    status_code=status.HTTP_201_CREATED,
)
async def add_bookmark(req: AddBookmarkRequest) -> BookmarkPr:
    """Parse the URL, fetch the PR's metadata via ``gh pr view``,
    flip ``is_bookmarked`` on the unified pr row.

    Refuses with 400 (bad URL or repo not configured), 404 (PR doesn't
    exist), 409 (already bookmarked), 502 (gh failure)."""
    pr_repo, pr_number = _parse_pr_url(req.url)

    # A PR is only worth bookmarking if we can act on it — i.e. its
    # repo is configured so it can be pulled down. Reject early and
    # point the user at the onboarding flow.
    config = load_config()
    if not is_repo_configured(pr_repo, configured_repos_index(config.repos)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Repo '{pr_repo}' isn't configured — add it first via "
            "'Add a repo'.",
        )

    # Pre-check for the 409 contract. The unified upsert is MAX/COALESCE,
    # so it would silently absorb a duplicate-bookmark POST without
    # this guard.
    existing = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if existing is not None and existing.is_bookmarked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"PR {pr_repo}#{pr_number} is already bookmarked",
        )

    try:
        data = await run_gh_json(
            [
                "pr", "view", str(pr_number),
                "--repo", pr_repo,
                "--json", "title,author,url,state",
            ],
            swallow_errors=False,
        )
    except GhNotFound as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "`gh` CLI not on PATH. Install GitHub CLI to enable bookmarks.",
        ) from e
    except GhFailed as e:
        # ``GhFailed.stderr`` carries the actual gh stderr; ``str(e)``
        # is unreliable because the exception overrides ``self.args``
        # with the gh argv. "Could not resolve to a PullRequest" is
        # GitHub's GraphQL response for a PR number that doesn't exist;
        # surface that as 404 instead of a generic 502.
        lower = e.stderr.lower()
        if (
            "could not resolve" in lower
            or "no pull request" in lower
            or "not found" in lower
        ):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no PR {pr_repo}#{pr_number} found on GitHub",
            ) from e
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    if data is None:
        # run_gh_json returns None when gh reports "no pull requests
        # found" without a non-zero exit (rare for `pr view` against a
        # specific number, but treat defensively).
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no PR {pr_repo}#{pr_number} found on GitHub",
        )

    if not isinstance(data, dict):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"`gh pr view {pr_number}` returned no payload",
        )

    title = data.get("title")
    if not isinstance(title, str):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "`gh pr view` did not return a title",
        )
    author = (data.get("author") or {}).get("login") or ""
    url = data.get("url") or req.url
    pr_state_value = _normalize_state(data.get("state"))

    ticket = extract_ticket(title, config.repos)

    now = now_iso()
    row = PrRow(
        pr_repo=pr_repo,
        pr_number=pr_number,
        is_bookmarked=True,
        bookmarked_at=now,
        title=title,
        author_login=author,
        url=url,
        ticket=ticket,
        state=pr_state_value,
        last_refreshed_at=now,
    )
    await asyncio.to_thread(pr_db.upsert_pr_sync, row)

    # Re-read to pick up any prior fields (notes, pr_state) that the
    # upsert preserved via COALESCE / LEFT JOIN.
    fresh = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if fresh is None:
        # Should be impossible — we just upserted. Raised as 502 so
        # the user sees the failure mode (DB race / disk full) rather
        # than a silent 500.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"bookmark write for {pr_repo}#{pr_number} did not persist",
        )
    return _payload_from_row(fresh)


# ---------------------------------------------------------------------
# Pull-down (mirrors the authored-PR route)
# ---------------------------------------------------------------------


@router.post(
    "/bookmarks/{pr_repo:path}/{pr_number}/pull-down",
    response_model=PullDownResponse,
)
async def pull_down_bookmark(pr_repo: str, pr_number: int) -> PullDownResponse:
    """Fetch the bookmarked PR's branch and create a worktree.

    Looks up the bookmark row to capture ``author_login`` (so the new
    worktree carries it for the hub's REVIEWING-tier split). Refuses
    with 404 if the PR isn't bookmarked.

    On success the worktree row exists and the bookmark row also
    stays (a worktree-backed PR can still be bookmarked — symmetric
    with the rest of the model). If the user wants the bookmark gone
    after pull-down, they can click Unbookmark.
    """
    row = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if row is None or not row.is_bookmarked:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} is not bookmarked",
        )
    return await perform_pull_down(
        pr_repo, pr_number, author_login=row.author_login
    )


# ---------------------------------------------------------------------
# Delete (unbookmark)
# ---------------------------------------------------------------------


class DeleteBookmarkResponse(BaseModel):
    deleted: Literal[True] = True


@router.delete(
    "/bookmarks/{pr_repo:path}/{pr_number}",
    response_model=DeleteBookmarkResponse,
)
async def delete_bookmark(
    pr_repo: str, pr_number: int
) -> DeleteBookmarkResponse:
    existing = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if existing is None or not existing.is_bookmarked:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} is not bookmarked",
        )
    await asyncio.to_thread(
        pr_db.set_bookmark_flag_sync, pr_repo, pr_number, False
    )
    await asyncio.to_thread(pr_db.maybe_gc_sync, pr_repo, pr_number)
    return DeleteBookmarkResponse()


# ---------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------


class UpdateNotesRequest(BaseModel):
    notes: str = Field(..., max_length=_NOTES_MAX_LENGTH)


class UpdateNotesResponse(BaseModel):
    notes: str


@router.put(
    "/bookmarks/{pr_repo:path}/{pr_number}/notes",
    response_model=UpdateNotesResponse,
)
async def update_notes(
    pr_repo: str, pr_number: int, req: UpdateNotesRequest
) -> UpdateNotesResponse:
    # 404 if the row exists but isn't a bookmark — preserves the
    # surface-scoped semantics the shim's `update_bookmark_notes_sync`
    # used to enforce via `AND is_bookmarked = 1`.
    existing = await asyncio.to_thread(pr_db.get_pr_sync, pr_repo, pr_number)
    if existing is None or not existing.is_bookmarked:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} is not bookmarked",
        )
    await asyncio.to_thread(
        pr_db.update_notes_sync, pr_repo, pr_number, req.notes
    )
    return UpdateNotesResponse(notes=req.notes)
