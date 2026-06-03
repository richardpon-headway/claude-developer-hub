"""Unified workspace list endpoint.

`GET /api/workspaces` returns one entity per workspace, deduped by PR
identity, for the two-bucket hub (My Work / Reviewing). It folds the
three legacy read surfaces — worktrees, bookmarks, authored PRs — into
a single list:

- every ``worktree`` row (PR-linked or a no-PR branch),
- every bookmarked ``pr`` row with no worktree,
- every authored, open ``pr`` row with no worktree.

Dedup key is ``(pr_repo, pr_number)``; a PR that has a worktree wins
over its bookmark/authored entry (the worktree row carries the local
state and the merged ``is_bookmarked``). No-PR worktrees key on their
own identity and never collide.

The endpoint is deliberately dumb: it emits raw fields + ``user_login``
and lets the frontend derive bucket (authorship) and lifecycle tier.
Both the rich ``pr_state`` and the synchronously-written scalar columns
(``state`` / ``ci_status`` / ``is_draft``) are surfaced so the card can
render a lifecycle chip immediately, before the (10-min) enrichment
poll fills ``pr_state``.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from app.models.pr import PrCiStatus, PrRow, PrState
from app.models.worktree import PrStateSummary, WorktreeRow, WorktreeStatus
from app.services import pr_db
from app.services import worktree as wt_svc
from app.services.gh_identity import get_user_login

router = APIRouter(prefix="/api", tags=["workspaces"])


class WorktreeBrief(BaseModel):
    """The local-checkout facet of a workspace. Absent on non-local
    entities (a bookmarked or authored PR not yet pulled down)."""

    repo: str
    name: str
    path: str
    branch: str
    status: WorktreeStatus
    has_claude_session: bool


class WorkspaceEntity(BaseModel):
    """One workspace. Bucket (My Work / Reviewing) and lifecycle tier
    are derived by the frontend from ``author_login`` / ``user_login``
    and ``pr_state``/scalars respectively."""

    pr_repo: str | None = None
    pr_number: int | None = None
    title: str
    url: str
    author_login: str | None = None
    is_bookmarked: bool = False
    # Synchronously-written scalars — the chip fallback when pr_state
    # hasn't been enriched yet.
    state: PrState | None = None
    ci_status: PrCiStatus | None = None
    is_draft: bool = False
    ticket: str | None = None
    notes: str | None = None
    worktree: WorktreeBrief | None = None
    pr_state: PrStateSummary | None = None


class WorkspacesResponse(BaseModel):
    user_login: str | None = None
    workspaces: list[WorkspaceEntity]


def _pr_url(pr_repo: str | None, pr_number: int | None, fallback: str) -> str:
    if fallback:
        return fallback
    if pr_repo and pr_number is not None:
        return f"https://github.com/{pr_repo}/pull/{pr_number}"
    return ""


def _entity_from_worktree(w: WorktreeRow, pr: PrRow | None) -> WorkspaceEntity:
    title = (
        (w.pr_state.title if w.pr_state and w.pr_state.title else None)
        or (pr.title if pr else None)
        or w.name
    )
    return WorkspaceEntity(
        pr_repo=w.pr_repo,
        pr_number=w.pr_number,
        title=title,
        url=_pr_url(w.pr_repo, w.pr_number, (pr.url if pr else None) or ""),
        # author_login projected from the pr row; falls back to the
        # worktree's JOIN projection. None ⇒ frontend buckets to My Work.
        author_login=(pr.author_login if pr else None) or w.pr_author_login,
        is_bookmarked=bool(pr.is_bookmarked) if pr else False,
        state=pr.state if pr else None,
        ci_status=pr.ci_status if pr else None,
        is_draft=bool(pr.is_draft) if pr else False,
        ticket=w.ticket or (pr.ticket if pr else None),
        # A worktree's notes are per-checkout (the editor saves to the
        # worktree row), so they win over the pr row's notes here.
        notes=w.notes,
        worktree=WorktreeBrief(
            repo=w.repo,
            name=w.name,
            path=w.path,
            branch=w.branch,
            status=w.status,
            has_claude_session=w.has_claude_session,
        ),
        pr_state=w.pr_state,
    )


def _entity_from_pr(pr: PrRow) -> WorkspaceEntity:
    title = (
        pr.title
        or (pr.pr_state.title if pr.pr_state else None)
        or ""
    )
    return WorkspaceEntity(
        pr_repo=pr.pr_repo,
        pr_number=pr.pr_number,
        title=title,
        url=pr.url or "",
        author_login=pr.author_login,
        is_bookmarked=pr.is_bookmarked,
        state=pr.state,
        ci_status=pr.ci_status,
        is_draft=pr.is_draft,
        ticket=pr.ticket,
        notes=pr.notes,
        worktree=None,
        pr_state=pr.pr_state,
    )


@router.get("/workspaces", response_model=WorkspacesResponse)
async def list_workspaces() -> WorkspacesResponse:
    prs, worktrees, user_login = await asyncio.gather(
        asyncio.to_thread(pr_db.list_pr_sync),
        asyncio.to_thread(wt_svc.list_worktrees_sync),
        get_user_login(),
    )
    pr_by_key: dict[tuple[str, int], PrRow] = {
        (p.pr_repo, p.pr_number): p for p in prs
    }

    workspaces: list[WorkspaceEntity] = []
    attached: set[tuple[str, int]] = set()

    # Worktree entities first — a worktree wins any PR-identity collision.
    for w in worktrees:
        pr: PrRow | None = None
        if w.pr_repo is not None and w.pr_number is not None:
            key = (w.pr_repo, w.pr_number)
            attached.add(key)
            pr = pr_by_key.get(key)
        workspaces.append(_entity_from_worktree(w, pr))

    # PR-only entities (no worktree) that some surface still tracks:
    # bookmarked, or your own still-open authored PR. Iterating the pr
    # list once dedupes a PR that is both bookmarked and authored-by-me.
    for p in prs:
        if (p.pr_repo, p.pr_number) in attached:
            continue
        authored_open = (
            user_login is not None
            and p.author_login == user_login
            and p.state == "open"
        )
        if p.is_bookmarked or authored_open:
            workspaces.append(_entity_from_pr(p))

    return WorkspacesResponse(user_login=user_login, workspaces=workspaces)
