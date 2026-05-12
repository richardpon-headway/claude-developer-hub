"""FastAPI entrypoint.

The lifespan hook applies any pending SQLite migrations and launches the
long-lived iTerm2 supervisor task. The migration runner is real as of
Slice D; the supervisor is still a stub pending Slice F.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.db import apply_migrations
from app.routes import repos, workspace, worktrees
from app.services.iterm_supervisor import iterm_supervisor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await apply_migrations()
    supervisor_task = asyncio.create_task(iterm_supervisor(app.state))
    try:
        yield
    finally:
        supervisor_task.cancel()
        try:
            await supervisor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Claude Developer Hub", lifespan=lifespan)

app.include_router(repos.router)
app.include_router(worktrees.router)
app.include_router(workspace.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=47823, reload=True)


if __name__ == "__main__":
    main()
