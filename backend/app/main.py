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
    config,
    inbox,
    repos,
    skills,
    token_usage,
    workspace,
    worktrees,
)
from app.services.inbox_poll import inbox_poll_loop
from app.services.iterm_supervisor import iterm_supervisor
from app.services.pr_state_poll import pr_state_poll_loop


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await apply_migrations()
    supervisor_task = asyncio.create_task(iterm_supervisor(app.state))
    pr_state_task = asyncio.create_task(pr_state_poll_loop(app.state))
    inbox_task = asyncio.create_task(inbox_poll_loop(app.state))
    background_tasks = (supervisor_task, pr_state_task, inbox_task)
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
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
app.include_router(inbox.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=47823, reload=True)


if __name__ == "__main__":
    main()
