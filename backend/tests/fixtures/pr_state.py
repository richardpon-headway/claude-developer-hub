"""pr_state row seeder for tests.

The pr_state table is keyed on (pr_repo, pr_number) and FKs to the
pr table. Callers must ensure a matching pr row exists first — use
:func:`tests.fixtures.pr.seed_pr` for that.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def seed_pr_state(
    db_path: Path,
    *,
    pr_repo: str,
    pr_number: int,
    headline: str = "ready_to_merge",
    extra_payload: dict[str, Any] | None = None,
    checked_at: str = "2026-05-14T00:00:00Z",
) -> None:
    """Insert one pr_state row with a sensible default payload shape.

    Caller must ensure a matching pr row exists first (FK constraint).
    """
    payload: dict[str, Any] = {
        "pr_number": pr_number,
        "url": f"https://github.com/{pr_repo}/pull/{pr_number}",
    }
    if extra_payload:
        payload.update(extra_payload)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO pr_state (pr_repo, pr_number, headline, payload, "
            "checked_at) VALUES (?, ?, ?, ?, ?)",
            (pr_repo, pr_number, headline, json.dumps(payload), checked_at),
        )
        conn.commit()
    finally:
        conn.close()
