"""HTTP endpoints for the todo widget.

A flat global checklist. No repo/worktree scoping — these are the
user's own tasks. Edits autosave from the frontend (no explicit save),
so ``PATCH`` is the workhorse: the client sends whichever of
title/bullets/done changed.

- ``GET    /api/widgets/todo/items``        — the full list, split into
  pending (drag order) and completed (most-recent-first).
- ``POST   /api/widgets/todo/items``        — create (defaults to an
  empty card appended to the bottom of pending).
- ``PATCH  /api/widgets/todo/items/{id}``   — partial update.
- ``DELETE /api/widgets/todo/items/{id}``   — remove.
- ``POST   /api/widgets/todo/reorder``      — new pending order by id;
  returns the refreshed list.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status

from app.widgets.todo import store
from app.widgets.todo.models import (
    CreateTodoRequest,
    DeleteTodoResponse,
    ReorderRequest,
    TodoItem,
    TodoList,
    UpdateTodoRequest,
)

router = APIRouter(prefix="/api/widgets/todo", tags=["todo-widget"])


@router.get("/items", response_model=TodoList)
async def list_items() -> TodoList:
    return await asyncio.to_thread(store.list_todos_sync)


@router.post("/items", response_model=TodoItem, status_code=status.HTTP_201_CREATED)
async def create_item(req: CreateTodoRequest) -> TodoItem:
    return await asyncio.to_thread(store.create_todo_sync, req.title, req.bullets)


@router.patch("/items/{item_id}", response_model=TodoItem)
async def update_item(item_id: int, req: UpdateTodoRequest) -> TodoItem:
    item = await asyncio.to_thread(
        store.update_todo_sync,
        item_id,
        title=req.title,
        bullets=req.bullets,
        done=req.done,
    )
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no todo item {item_id}")
    return item


@router.delete("/items/{item_id}", response_model=DeleteTodoResponse)
async def delete_item(item_id: int) -> DeleteTodoResponse:
    deleted = await asyncio.to_thread(store.delete_todo_sync, item_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no todo item {item_id}")
    return DeleteTodoResponse()


@router.post("/reorder", response_model=TodoList)
async def reorder(req: ReorderRequest) -> TodoList:
    await asyncio.to_thread(store.reorder_sync, req.ids)
    return await asyncio.to_thread(store.list_todos_sync)
