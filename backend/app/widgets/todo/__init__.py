"""Todo widget — a single global checklist in the hub's right rail.

Public surface for ``app.main`` to mount: the FastAPI ``router``.
"""

from __future__ import annotations

from app.widgets.todo.routes import router

__all__ = ["router"]
