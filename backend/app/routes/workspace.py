"""Workspace URL routing helpers.

``GET /api/workspace/from-path`` is what the ``cdh`` shell function calls:
given the user's ``pwd``, return the workspace URL that should open in
the browser. If the cwd matches no configured worktree, fall back to
the hub URL — that way dropping into any directory still opens
something useful.

Workspace URL shape is ``/workspace/<repo>/<name>`` (plan §4).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.db import open_db

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


class FromPathResponse(BaseModel):
    url: str


def _lookup_by_path_sync(absolute_path: str) -> tuple[str, str] | None:
    """Exact-match lookup against the worktree table. Returns
    ``(repo, name)`` or None."""
    conn = open_db()
    try:
        row = conn.execute(
            "SELECT repo, name FROM worktree WHERE path = ?",
            (absolute_path,),
        ).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


@router.get("/from-path", response_model=FromPathResponse)
async def from_path(path: str = Query(..., min_length=1)) -> FromPathResponse:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "path must be absolute",
        )
    abs_path = str(p.resolve())
    match = await asyncio.to_thread(_lookup_by_path_sync, abs_path)
    if match is not None:
        repo, name = match
        return FromPathResponse(url=f"/workspace/{repo}/{name}")
    return FromPathResponse(url="/")
