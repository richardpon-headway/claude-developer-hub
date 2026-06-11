"""Wire + storage models for the todo widget."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StringConstraints

# An item's title is free-form, multi-line text. The cap is generous (it
# may hold several lines plus a pasted URL) but bounded so a runaway
# paste can't bloat the row.
_TITLE_MAX = 10_000


class TodoItem(BaseModel):
    """One todo card."""

    id: int
    title: str
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


class UpdateTodoRequest(BaseModel):
    """PATCH semantics — every field optional.

    ``title`` carries autosaved edits; ``done`` toggles the item between
    the pending and completed sections. Omitted fields are left
    untouched.
    """

    title: Annotated[str, StringConstraints(max_length=_TITLE_MAX)] | None = None
    done: bool | None = None


class ReorderRequest(BaseModel):
    """New top-to-bottom order for the pending items, by id."""

    ids: list[int]


class DeleteTodoResponse(BaseModel):
    deleted: bool = True
