"""SQLite connection + migration runner.

Slice A ships a stub: it ensures the parent directory exists. The full runner
(scan ``backend/app/migrations/NNN_*.sql``, apply unapplied entries inside a
transaction, record in ``_migration``) lands in Slice D.
"""
from __future__ import annotations

from pathlib import Path

DB_PATH = Path.home() / "Library" / "Application Support" / "cdh" / "cdh.db"


async def apply_migrations() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
