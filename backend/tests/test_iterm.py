"""Tests for the iTerm2 connection supervisor + spawn function + endpoint.

The ``iterm2`` Python package is in deps but talks to a real iTerm2
process via a unix socket; unit tests mock the relevant pieces at the
``iterm2`` module level. Live-iTerm2 coverage lives under
``make iterm-smoke`` (not in CI).
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.config.schema import ITermWindow
from app.main import app
from app.services import iterm_spawn
from app.services import iterm_supervisor as supervisor
from app.services import worktree as wsvc

# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    wsvc._logs.clear()
    return {"db_path": db_path, "config_path": config_path, "dev_root": dev_root}


def _write_minimal_config(config_path: Path, dev_root: Path) -> None:
    config_path.write_text(
        yaml.safe_dump(
            {
                "development_root": str(dev_root),
                "repos": [],
                "iterm2": {
                    "default_window": {"width": 800, "height": 600, "x": 10, "y": 20}
                },
            }
        )
    )


def _seed_worktree(db_path: Path, repo: str, name: str, path: Path) -> None:
    """Insert a 'ready' worktree row without going through the service."""
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo, name, str(path), "main", "2026-01-01T00:00:00Z", "ready"),
        )
        conn.commit()
    finally:
        conn.close()


def _build_fake_window(
    window_id: str = "W1",
    claude_session_id: str = "S-claude",
    shell_session_id: str = "S-shell",
) -> MagicMock:
    """Construct the nested mock structure that spawn_worktree_window
    walks: window → current_tab → current_session, plus async_create_tab
    → tab → current_session."""
    claude_session = MagicMock(session_id=claude_session_id)
    claude_session.async_send_text = AsyncMock()
    claude_tab = MagicMock(current_session=claude_session)

    shell_session = MagicMock(session_id=shell_session_id)
    shell_session.async_send_text = AsyncMock()
    shell_tab = MagicMock(current_session=shell_session)

    window = MagicMock(window_id=window_id, current_tab=claude_tab)
    window.async_set_frame = AsyncMock()
    window.async_create_tab = AsyncMock(return_value=shell_tab)
    return window


# --- spawn_worktree_window unit test -------------------------------------


def test_spawn_worktree_window_calls_iterm_api(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    import iterm2

    fake_window = _build_fake_window()
    # Real API: iterm2.Window.async_create(connection, profile=...) → Window
    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )

    frame = ITermWindow(width=800, height=600, x=10, y=20)
    fake_conn = MagicMock()
    worktree_path = _isolate["dev_root"] / "wt"
    worktree_path.mkdir()

    result = asyncio.run(
        iterm_spawn.spawn_worktree_window(fake_conn, worktree_path, frame)
    )

    assert result.window_id == "W1"
    assert result.claude_session_id == "S-claude"
    assert result.shell_session_id == "S-shell"

    # First tab: cd + claude
    claude_call = fake_window.current_tab.current_session.async_send_text.await_args
    assert claude_call.args[0] == f"cd {worktree_path}\nclaude\n"
    # Second tab: cd only
    shell_tab = fake_window.async_create_tab.return_value
    shell_call = shell_tab.current_session.async_send_text.await_args
    assert shell_call.args[0] == f"cd {worktree_path}\n"
    # Frame applied
    fake_window.async_set_frame.assert_awaited_once()


# --- upsert_iterm_sessions_sync ------------------------------------------


def test_upsert_iterm_sessions_replaces_prior_rows(_isolate: dict[str, Path]) -> None:
    repo, name = "myapp", "feature"
    # FK requires the worktree row to exist first.
    _seed_worktree(_isolate["db_path"], repo, name, _isolate["dev_root"] / "wt")

    result1 = iterm_spawn.SpawnResult(window_id="W1", claude_session_id="A1", shell_session_id="A2")
    iterm_spawn.upsert_iterm_sessions_sync(repo, name, result1)

    # Spawn again with new ids — should overwrite, not duplicate
    result2 = iterm_spawn.SpawnResult(window_id="W2", claude_session_id="B1", shell_session_id="B2")
    iterm_spawn.upsert_iterm_sessions_sync(repo, name, result2)

    conn = sqlite3.connect(_isolate["db_path"])
    try:
        rows = dict(
            conn.execute(
                "SELECT role, iterm_session_id FROM iterm_session WHERE repo=? AND worktree_name=?",
                (repo, name),
            )
        )
    finally:
        conn.close()
    assert rows == {"claude": "B1", "shell": "B2"}


# --- restart detection ---------------------------------------------------


def test_restart_invalidates_sessions(_isolate: dict[str, Path]) -> None:
    # Seed a worktree + iterm_session rows + a persisted started_at
    repo, name = "r", "wt"
    _seed_worktree(_isolate["db_path"], repo, name, _isolate["dev_root"] / "wt")
    iterm_spawn.upsert_iterm_sessions_sync(
        repo,
        name,
        iterm_spawn.SpawnResult(window_id="W1", claude_session_id="S1", shell_session_id="S2"),
    )
    supervisor._write_persisted_started_at_sync("OLD")

    # Build a fake connection whose iterm2_started_at probe returns "NEW".
    fake_app = MagicMock()
    fake_app.async_get_variable = AsyncMock(return_value="NEW")
    import unittest.mock as _m

    import iterm2
    with _m.patch.object(iterm2, "async_get_app", AsyncMock(return_value=fake_app)):
        asyncio.run(supervisor._detect_and_handle_restart(MagicMock()))

    # Sessions should have been wiped + persisted value updated
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        n = conn.execute("SELECT COUNT(*) FROM iterm_session").fetchone()[0]
        persisted = conn.execute(
            "SELECT value FROM iterm_lifecycle WHERE key='iterm2_started_at'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    assert persisted == "NEW"


def test_first_connect_records_started_at_without_invalidating(
    _isolate: dict[str, Path],
) -> None:
    repo, name = "r", "wt"
    _seed_worktree(_isolate["db_path"], repo, name, _isolate["dev_root"] / "wt")
    iterm_spawn.upsert_iterm_sessions_sync(
        repo,
        name,
        iterm_spawn.SpawnResult(window_id="W1", claude_session_id="S1", shell_session_id="S2"),
    )

    fake_app = MagicMock()
    fake_app.async_get_variable = AsyncMock(return_value="START-1")
    import unittest.mock as _m

    import iterm2

    with _m.patch.object(iterm2, "async_get_app", AsyncMock(return_value=fake_app)):
        asyncio.run(supervisor._detect_and_handle_restart(MagicMock()))

    conn = sqlite3.connect(_isolate["db_path"])
    try:
        n = conn.execute("SELECT COUNT(*) FROM iterm_session").fetchone()[0]
        persisted = conn.execute(
            "SELECT value FROM iterm_lifecycle WHERE key='iterm2_started_at'"
        ).fetchone()[0]
    finally:
        conn.close()
    # Same value → no invalidation; rows still there
    assert n == 2
    assert persisted == "START-1"


# --- POST /api/worktree/{repo}/{name}/spawn-iterm ------------------------


def test_spawn_endpoint_503_when_not_connected(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    _seed_worktree(_isolate["db_path"], "r", "wt", _isolate["dev_root"] / "wt")

    with TestClient(app) as client:
        # Force the supervisor's state to "disconnected" before issuing the request.
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post("/api/worktree/r/wt/spawn-iterm")
    assert r.status_code == 503
    assert "Python API" in r.json()["detail"]


def test_spawn_endpoint_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/worktree/missing/wt/spawn-iterm")
    assert r.status_code == 404


def test_spawn_endpoint_400_when_path_missing(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    repo, name = "r", "wt"
    missing_path = _isolate["dev_root"] / "does-not-exist"
    # Seed row pointing at a path that doesn't exist on disk
    conn = sqlite3.connect(_isolate["db_path"])
    conn.execute(
        "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo, name, str(missing_path), "main", "2026-01-01T00:00:00Z", "ready"),
    )
    conn.commit()
    conn.close()

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(f"/api/worktree/{repo}/{name}/spawn-iterm")
    assert r.status_code == 400
    assert "missing on disk" in r.json()["detail"]


def test_spawn_endpoint_happy_path(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    repo, name = "r", "wt"
    _seed_worktree(_isolate["db_path"], repo, name, _isolate["dev_root"] / "wt")

    fake_window = _build_fake_window(
        window_id="W42", claude_session_id="C42", shell_session_id="SH42"
    )
    import iterm2

    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    # Stub the discovery so this test doesn't wait the full 30s for a
    # jsonl that won't appear; that's exercised in test_sidecar.py.
    import app.routes.worktrees as wt_route

    monkeypatch.setattr(wt_route, "discover_session_id", AsyncMock(return_value=None))

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(f"/api/worktree/{repo}/{name}/spawn-iterm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_id"] == "W42"
    assert body["claude_session_id"] == "C42"
    assert body["shell_session_id"] == "SH42"
    assert body["claude_session_uuid"] is None
    assert body["sidecar_path"] is None

    # iterm_session rows persisted
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        rows = dict(
            conn.execute(
                "SELECT role, iterm_session_id FROM iterm_session "
                "WHERE repo=? AND worktree_name=?",
                (repo, name),
            )
        )
    finally:
        conn.close()
    assert rows == {"claude": "C42", "shell": "SH42"}


def test_spawn_endpoint_502_on_iterm_error(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    _seed_worktree(_isolate["db_path"], "r", "wt", _isolate["dev_root"] / "wt")

    import iterm2

    monkeypatch.setattr(
        iterm2.Window,
        "async_create",
        AsyncMock(side_effect=RuntimeError("simulated rpc failure")),
    )

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/worktree/r/wt/spawn-iterm")
    assert r.status_code == 502
    assert "iTerm2 spawn failed" in r.json()["detail"]
    assert "simulated rpc failure" in r.json()["detail"]
