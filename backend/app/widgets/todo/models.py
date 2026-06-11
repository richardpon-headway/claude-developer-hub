"""Wire + storage models for the todo widget."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# A title and each bullet are free-form single fields. The caps are
# generous (a title might hold a pasted URL) but bounded so a runaway
# paste can't bloat the row.
_TITLE_MAX = 2_000
_BULLET_MAX = 2_000
_MAX_BULLETS = 100

Bullet = Annotated[str, StringConstraints(max_length=_BULLET_MAX)]


class TodoItem(BaseModel):
    """One todo card."""

    id: int
    title: str
    bullets: list[str]
    done: bool
    sort_order: float
    completed_at: str | None = None
    created_at: str


class TodoList(BaseModel):
    """The full widget payload, pre-split into the two rendered sections."""

    # Pending items in drag order (sort_order asc).
    pending: list[TodoItem]
    # Completed items, most recently finished first (completed_at desc).
    completed: list[TodoItem]


class CreateTodoRequest(BaseModel):
    title: Annotated[str, StringConstraints(max_length=_TITLE_MAX)] = ""
    bullets: list[Bullet] = Field(default_factory=list, max_length=_MAX_BULLETS)


class UpdateTodoRequest(BaseModel):
    """PATCH semantics — every field optional.

    ``title`` / ``bullets`` carry autosaved edits; ``done`` toggles the
    item between the pending and completed sections. Omitted fields are
    left untouched.
    """

    title: Annotated[str, StringConstraints(max_length=_TITLE_MAX)] | None = None
    bullets: list[Bullet] | None = Field(default=None, max_length=_MAX_BULLETS)
    done: bool | None = None


class ReorderRequest(BaseModel):
    """New top-to-bottom order for the pending items, by id."""

    ids: list[int]


class DeleteTodoResponse(BaseModel):
    deleted: bool = True
