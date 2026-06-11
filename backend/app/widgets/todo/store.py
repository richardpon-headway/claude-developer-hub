"""SQLite access for the todo widget.

All functions are synchronous (sqlite3 is sync-only); the route layer
offloads them to a thread. Each call opens and closes its own
connection, mirroring ``app.services.pr_db``.

``bullets`` round-trips as a JSON array of strings — encoded on write,
decoded on read — so the rest of the stack works with a real
``list[str]`` and never sees the storage encoding.
"""

from __future__ import annotations

import json
import sqlite3

from app.db import open_db
from app.models.worktree import now_iso
from app.widgets.todo.models import TodoItem, TodoList


def _row_to_item(row: sqlite3.Row) -> TodoItem:
    raw = row["bullets"]
    try:
        bullets = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        # Defensive: a hand-edited DB row shouldn't crash the list.
        bullets = []
    if not isinstance(bullets, list):
        bullets = []
    return TodoItem(
        id=row["id"],
        title=row["title"],
        bullets=[str(b) for b in bullets],
        done=bool(row["done"]),
        sort_order=row["sort_order"],
        completed_at=row["completed_at"],
        created_at=row["created_at"],
    )


def list_todos_sync() -> TodoList:
    conn = open_db()
    try:
        conn.row_factory = sqlite3.Row
        pending_rows = conn.execute(
            "SELECT * FROM todo WHERE done = 0 ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        completed_rows = conn.execute(
            "SELECT * FROM todo WHERE done = 1 ORDER BY completed_at DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()
    return TodoList(
        pending=[_row_to_item(r) for r in pending_rows],
        completed=[_row_to_item(r) for r in completed_rows],
    )


def _next_pending_sort_order(conn: sqlite3.Connection) -> float:
    """One past the current bottom of the pending list."""
    row = conn.execute("SELECT MAX(sort_order) FROM todo WHERE done = 0").fetchone()
    current_max = row[0]
    return (current_max + 1.0) if current_max is not None else 0.0


def create_todo_sync(title: str, bullets: list[str]) -> TodoItem:
    conn = open_db()
    try:
        conn.row_factory = sqlite3.Row
        sort_order = _next_pending_sort_order(conn)
        cur = conn.execute(
            "INSERT INTO todo (title, bullets, done, sort_order, created_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (title, json.dumps(bullets), sort_order, now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM todo WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _row_to_item(row)


def update_todo_sync(
    item_id: int,
    *,
    title: str | None,
    bullets: list[str] | None,
    done: bool | None,
) -> TodoItem | None:
    """Apply a partial update. Returns the fresh item, or None if the
    id doesn't exist.

    A ``done`` transition is the interesting case:
    - pending → done: stamp ``completed_at`` so the completed section
      can sort by it.
    - done → pending: clear ``completed_at`` and move the item to the
      bottom of the pending list (so it doesn't reappear mid-order).
    """
    conn = open_db()
    try:
        conn.row_factory = sqlite3.Row
        existing = conn.execute("SELECT * FROM todo WHERE id = ?", (item_id,)).fetchone()
        if existing is None:
            return None

        sets: list[str] = []
        params: list[object] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if bullets is not None:
            sets.append("bullets = ?")
            params.append(json.dumps(bullets))
        if done is not None and bool(done) != bool(existing["done"]):
            sets.append("done = ?")
            params.append(1 if done else 0)
            if done:
                sets.append("completed_at = ?")
                params.append(now_iso())
            else:
                sets.append("completed_at = NULL")
                sets.append("sort_order = ?")
                params.append(_next_pending_sort_order(conn))

        if sets:
            params.append(item_id)
            conn.execute(f"UPDATE todo SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()

        row = conn.execute("SELECT * FROM todo WHERE id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_item(row)


def delete_todo_sync(item_id: int) -> bool:
    conn = open_db()
    try:
        cur = conn.execute("DELETE FROM todo WHERE id = ?", (item_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reorder_sync(ids: list[int]) -> None:
    """Rewrite the pending items' sort_order to match ``ids``.

    Only ids that exist and are still pending are repositioned;
    unknown or already-completed ids are ignored. Any pending item not
    named in ``ids`` keeps its row but is pushed below the explicitly
    ordered set (it sorts after, by id) — so a stale client list can't
    silently drop an item from view.
    """
    conn = open_db()
    try:
        for position, item_id in enumerate(ids):
            conn.execute(
                "UPDATE todo SET sort_order = ? WHERE id = ? AND done = 0",
                (float(position), item_id),
            )
        conn.commit()
    finally:
        conn.close()
