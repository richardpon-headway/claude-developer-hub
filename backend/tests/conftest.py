"""Pytest config + the one autouse fixture every test file needs.

Tests use ``tmp_path``-scoped SQLite + config files driven by the
``CDH_DB_PATH`` / ``CDH_CONFIG_PATH`` env vars the production code
already supports. Each test gets a fresh DB with all migrations
applied. Test files don't need to redefine this themselves — the
per-file ``_isolate`` fixture in each ``test_*.py`` predates this
module and will be deleted in a follow-up commit. While both exist,
pytest's name-resolution prefers the closest definition, so behavior
is identical.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.services import worktree as wsvc


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Per-test isolation: fresh SQLite DB + tmp config file + tmp
    development_root, plus a clean in-memory log buffer.

    Returned dict is what tests destructure when they need the paths.
    The ``dev_root`` key wasn't included by every previous variant of
    this fixture; tests that don't use it simply ignore the key.
    """
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    # The in-memory log buffer + in-flight setup-task registry in the
    # worktree service can leak across tests when the same (repo, name)
    # key is reused; clear both. (Tasks should already be drained by
    # each test's explicit ``create_and_wait`` / ``wait_for_setup_complete``,
    # but the dict clear is cheap insurance against forgotten awaits.)
    wsvc._logs.clear()
    wsvc._setting_up_tasks.clear()
    return {"db_path": db_path, "config_path": config_path, "dev_root": dev_root}
