"""pr_state row seeder for tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def seed_pr_state(
    db_path: Path,
    repo: str,
    worktree_name: str,
    *,
    pr_number: int,
    pr_repo: str = "o/myapp",
    headline: str = "ready_to_merge",
    extra_payload: dict[str, Any] | None = None,
    checked_at: str = "2026-05-14T00:00:00Z",
) -> None:
    """Insert one pr_state row with a sensible default payload shape.

    Caller must ensure a matching worktree row exists first (FK).
    """
    payload: dict[str, Any] = {
        "pr_number": pr_number,
        "url": f"https://github.com/{pr_repo}/pull/{pr_number}",
    }
    if extra_payload:
        payload.update(extra_payload)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO pr_state (repo, worktree_name, headline, payload, "
            "checked_at) VALUES (?, ?, ?, ?, ?)",
            (repo, worktree_name, headline, json.dumps(payload), checked_at),
        )
        conn.commit()
    finally:
        conn.close()
