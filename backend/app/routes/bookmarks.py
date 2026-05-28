"""Bookmark HTTP endpoints (plan-48, Slice B).

Bookmarks are manually-added PR watches: paste a GitHub PR URL into
the hub, the backend fetches the PR's metadata via ``gh pr view`` and
persists a row in the ``bookmark`` table. Bookmarks render in their
own section alongside the inbox; the lifecycle is symmetric (explicit
add, explicit remove). Close / merge never auto-removes a bookmark —
the row keeps rendering with a ``closed`` / ``merged`` chip until the
user unbookmarks it.

- ``GET /api/bookmarks`` — list rows, newest-bookmarked first.
- ``POST /api/bookmarks`` — body ``{ url: string }``. Parses the URL,
  fetches via ``gh pr view``, inserts. Idempotent-by-error: a second
  POST for the same PR returns 409 (so the user knows their existing
  bookmark wasn't overwritten).
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
from app.models.bookmark import BookmarkRow, BookmarkState
from app.models.worktree import now_iso
from app.services import authored_pr_notes_db, bookmark_db
from app.services.bookmark_db import BookmarkExistsError
from app.services.gh_cli import GhFailed, GhNotFound, run_gh_json
from app.services.inbox_poll import _extract_ticket

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["bookmarks"])


# Soft cap on bookmark notes. Mirrors the inbox + worktree limits.
_NOTES_MAX_LENGTH = 10_000


_PR_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$"
)


# ---------------------------------------------------------------------
# Response payload
# ---------------------------------------------------------------------


class BookmarkPayload(BaseModel):
    """Wire shape for one bookmark row."""

    pr_repo: str
    pr_number: int
    title: str
    author_login: str
    url: str
    state: BookmarkState
    notes: str | None = None
    ticket: str | None = None
    bookmarked_at: str
    last_refreshed_at: str


class BookmarkListResponse(BaseModel):
    bookmarks: list[BookmarkPayload]


def _payload_from_row(row: BookmarkRow) -> BookmarkPayload:
    return BookmarkPayload(
        pr_repo=row.pr_repo,
        pr_number=row.pr_number,
        title=row.title,
        author_login=row.author_login,
        url=row.url,
        state=row.state,
        notes=row.notes,
        ticket=row.ticket,
        bookmarked_at=row.bookmarked_at,
        last_refreshed_at=row.last_refreshed_at,
    )


# ---------------------------------------------------------------------
# List
# ---------------------------------------------------------------------


@router.get("/bookmarks", response_model=BookmarkListResponse)
async def list_bookmarks() -> BookmarkListResponse:
    rows = await asyncio.to_thread(bookmark_db.list_bookmarks_sync)
    return BookmarkListResponse(bookmarks=[_payload_from_row(r) for r in rows])


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


def _normalize_state(value: object) -> BookmarkState:
    """Coerce ``gh pr view --json state`` to the BookmarkState enum.

    GitHub returns ``OPEN`` / ``CLOSED`` / ``MERGED``. Anything else
    defaults to ``closed`` (defensive — a future GitHub state shouldn't
    crash the bookmark add)."""
    s = str(value or "").lower()
    if s in ("open", "closed", "merged"):
        return s  # type: ignore[return-value]
    return "closed"


@router.post(
    "/bookmarks",
    response_model=BookmarkPayload,
    status_code=status.HTTP_201_CREATED,
)
async def add_bookmark(req: AddBookmarkRequest) -> BookmarkPayload:
    """Parse the URL, fetch the PR's metadata via ``gh pr view``,
    insert a bookmark row.

    Refuses with 400 (bad URL), 404 (PR doesn't exist), 409 (already
    bookmarked), 502 (gh failure)."""
    pr_repo, pr_number = _parse_pr_url(req.url)

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
    state = _normalize_state(data.get("state"))

    config = load_config()
    ticket = _extract_ticket(title, config.repos)

    # Surface transition: if this PR had notes attached on the
    # authored-PR tier, migrate them into the new bookmark row so the
    # user doesn't lose what they typed. Best-effort — a missing row
    # is the common case (most bookmarks are PRs the user discovered
    # outside the authored tier).
    authored_notes = await asyncio.to_thread(
        authored_pr_notes_db.get_notes_sync, pr_repo, pr_number
    )

    now = now_iso()
    row = BookmarkRow(
        pr_repo=pr_repo,
        pr_number=pr_number,
        title=title,
        author_login=author,
        url=url,
        state=state,
        notes=authored_notes,
        ticket=ticket,
        bookmarked_at=now,
        last_refreshed_at=now,
    )

    try:
        await asyncio.to_thread(bookmark_db.insert_bookmark_sync, row)
    except BookmarkExistsError as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"PR {pr_repo}#{pr_number} is already bookmarked",
        ) from e

    # Notes successfully copied into the bookmark — drop the source row.
    if authored_notes is not None:
        await asyncio.to_thread(
            authored_pr_notes_db.delete_notes_sync, pr_repo, pr_number
        )

    return _payload_from_row(row)


# ---------------------------------------------------------------------
# Pull-down (mirrors the authored-PR route — no inbox-row guard)
# ---------------------------------------------------------------------


# Lazy import to avoid a circular dep on routes/inbox at module load.
from app.routes.inbox import PullDownResponse, _perform_pull_down  # noqa: E402


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
    row = await asyncio.to_thread(
        bookmark_db.get_bookmark_sync, pr_repo, pr_number
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} is not bookmarked",
        )
    return await _perform_pull_down(
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
    affected = await asyncio.to_thread(
        bookmark_db.delete_bookmark_sync, pr_repo, pr_number
    )
    if affected == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} is not bookmarked",
        )
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
    affected = await asyncio.to_thread(
        bookmark_db.update_bookmark_notes_sync, pr_repo, pr_number, req.notes
    )
    if affected == 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"PR {pr_repo}#{pr_number} is not bookmarked",
        )
    return UpdateNotesResponse(notes=req.notes)
