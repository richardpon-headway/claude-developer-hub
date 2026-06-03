"""FastAPI entrypoint.

The lifespan hook applies any pending SQLite migrations and launches the
long-lived iTerm2 supervisor task. The migration runner is real as of
Slice D; the supervisor is still a stub pending Slice F.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import apply_migrations
from app.routes import (
    authored_prs,
    bookmarks,
    config,
    refresh,
    repos,
    skills,
    token_usage,
    workspace,
    worktrees,
)
from app.routes.worktrees import _post_spawn_tasks
from app.services.authored_poll import authored_poll_loop
from app.services.iterm_supervisor import iterm_supervisor
from app.services.pr_enrichment_poll import enrichment_poll_loop
from app.services.worktree import _setting_up_tasks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await apply_migrations()
    supervisor_task = asyncio.create_task(iterm_supervisor(app.state))
    enrichment_task = asyncio.create_task(enrichment_poll_loop(app.state))
    authored_task = asyncio.create_task(authored_poll_loop(app.state))
    background_tasks = (
        supervisor_task, enrichment_task, authored_task,
    )
    try:
        yield
    finally:
        # Snapshot the fire-and-forget task collections before
        # cancelling — their done-callbacks mutate the underlying
        # containers, so iterating live would hit "dict changed size
        # during iteration" once tasks start completing.
        in_flight = (
            list(background_tasks)
            + list(_setting_up_tasks.values())
            + list(_post_spawn_tasks)
        )
        for task in in_flight:
            task.cancel()
        for task in in_flight:
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Claude Developer Hub", lifespan=lifespan)

app.include_router(repos.router)
app.include_router(worktrees.router)
app.include_router(workspace.router)
app.include_router(token_usage.router)
app.include_router(config.router)
app.include_router(skills.router)
app.include_router(bookmarks.router)
app.include_router(authored_prs.router)
app.include_router(refresh.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=47823, reload=True)


if __name__ == "__main__":
    main()
