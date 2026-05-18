"""Tests for the sidecar slice: session-id discovery + sidecar write +
the spawn endpoint's integration of both."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import iterm_spawn, sidecar
from app.services.sidecar import (
    build_sidecar,
    discover_session_id,
    encode_project_dir,
    write_sidecar_sync,
)
from tests.fixtures.worktree import seed_worktree

# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    sidecar_dir = tmp_path / "sidecars"
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    # Point Claude's projects dir at a tmp location so tests don't read
    # the user's real ~/.claude/projects/.
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    db.apply_migrations_sync(db_path)
    config_path.write_text(
        yaml.safe_dump(
            {
                "development_root": str(dev_root),
                "repos": [],
                "iterm2": {"default_window": {"width": 800, "height": 600, "x": 0, "y": 0}},
                "token_monitor": {
                    "api_url": "http://localhost:47821",
                    "sidecar_dir": str(sidecar_dir),
                },
            }
        )
    )
    return {
        "db_path": db_path,
        "config_path": config_path,
        "dev_root": dev_root,
        "sidecar_dir": sidecar_dir,
        "fake_home": fake_home,
    }


def _make_fake_jsonl(
    fake_home: Path, worktree_path: Path, uuid: str, mtime: float | None = None
) -> Path:
    """Create ~/.claude/projects/<encoded(worktree)>/<uuid>.jsonl with the
    given mtime (defaults to now). Returns the file path."""
    proj_dir = fake_home / ".claude" / "projects" / encode_project_dir(worktree_path)
    proj_dir.mkdir(parents=True, exist_ok=True)
    f = proj_dir / f"{uuid}.jsonl"
    f.write_text("{}\n")
    if mtime is not None:
        import os
        os.utime(f, (mtime, mtime))
    return f


# --- encode_project_dir --------------------------------------------------


def test_encode_project_dir_replaces_slashes() -> None:
    assert encode_project_dir(Path("/Users/rpon/dev/x")) == "-Users-rpon-dev-x"
    assert encode_project_dir(Path("/a")) == "-a"
    # Periods, underscores, dashes pass through unchanged
    assert (
        encode_project_dir(Path("/a/b.c_d-e/f"))
        == "-a-b.c_d-e-f"
    )


# --- discover_session_id -------------------------------------------------


@pytest.mark.asyncio
async def test_discover_session_id_finds_new_jsonl(_isolate: dict[str, Path]) -> None:
    wt = _isolate["dev_root"] / "wt1"
    wt.mkdir()
    floor = time.time()
    # File appears between probes
    _make_fake_jsonl(
        _isolate["fake_home"], wt, "11111111-1111-1111-1111-111111111111",
        mtime=floor + 0.1,
    )
    sid = await discover_session_id(wt, floor, timeout=2.0, poll_interval=0.05)
    assert sid == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_discover_session_id_ignores_pre_floor_files(
    _isolate: dict[str, Path],
) -> None:
    wt = _isolate["dev_root"] / "wt2"
    wt.mkdir()
    # Pre-existing jsonl (e.g., from a prior claude session in this dir)
    _make_fake_jsonl(
        _isolate["fake_home"], wt, "old-uuid", mtime=time.time() - 100,
    )
    floor = time.time()
    sid = await discover_session_id(wt, floor, timeout=0.5, poll_interval=0.05)
    assert sid is None


@pytest.mark.asyncio
async def test_discover_session_id_picks_newest(_isolate: dict[str, Path]) -> None:
    wt = _isolate["dev_root"] / "wt3"
    wt.mkdir()
    floor = time.time()
    # Two jsonls both newer than floor; newest wins
    _make_fake_jsonl(_isolate["fake_home"], wt, "older", mtime=floor + 0.1)
    _make_fake_jsonl(_isolate["fake_home"], wt, "newer", mtime=floor + 0.5)
    sid = await discover_session_id(wt, floor, timeout=2.0, poll_interval=0.05)
    assert sid == "newer"


@pytest.mark.asyncio
async def test_discover_session_id_times_out(_isolate: dict[str, Path]) -> None:
    wt = _isolate["dev_root"] / "wt4"
    wt.mkdir()
    floor = time.time()
    # Nothing ever appears
    sid = await discover_session_id(wt, floor, timeout=0.3, poll_interval=0.05)
    assert sid is None


# --- build_sidecar -------------------------------------------------------


def test_build_sidecar_minimal() -> None:
    s = build_sidecar("abc-123", worktree="myrepo_feature")
    assert s["session_id"] == "abc-123"
    assert s["started_via"] == "cdh"
    assert s["worktree"] == "myrepo_feature"
    assert "started_at" in s
    # No empty / null keys
    assert "ticket" not in s
    assert "pr_number" not in s


def test_build_sidecar_full() -> None:
    s = build_sidecar(
        "abc",
        worktree="r_n",
        ticket="PROJ-12",
        pr_number=42,
        pr_repo="acme/repo",
        extra={"note": "from a test"},
    )
    assert s["ticket"] == "PROJ-12"
    assert s["pr_number"] == 42
    assert s["pr_repo"] == "acme/repo"
    assert s["metadata"] == {"note": "from a test"}


# --- write_sidecar_sync --------------------------------------------------


def test_write_sidecar_creates_dir_and_file(_isolate: dict[str, Path]) -> None:
    path = write_sidecar_sync("uuid-1", {"session_id": "uuid-1", "x": 1})
    assert path.exists()
    assert path.name == "uuid-1.json"
    assert json.loads(path.read_text()) == {"session_id": "uuid-1", "x": 1}


def test_write_sidecar_atomic(_isolate: dict[str, Path]) -> None:
    write_sidecar_sync("uuid-2", {"session_id": "uuid-2"})
    leftover = list(_isolate["sidecar_dir"].glob(".uuid-2.json.*.tmp"))
    assert leftover == []


# --- spawn endpoint integration ------------------------------------------


def _build_fake_window(
    window_id: str = "W1",
    claude_session_id: str = "S-claude",
    shell_session_id: str = "S-shell",
) -> MagicMock:
    claude_session = MagicMock(session_id=claude_session_id)
    claude_session.async_send_text = AsyncMock()
    claude_tab = MagicMock(current_session=claude_session)
    claude_tab.async_select = AsyncMock()

    shell_session = MagicMock(session_id=shell_session_id)
    shell_session.async_send_text = AsyncMock()
    shell_tab = MagicMock(current_session=shell_session)

    window = MagicMock(window_id=window_id, current_tab=claude_tab)
    window.async_set_frame = AsyncMock()
    window.async_create_tab = AsyncMock(return_value=shell_tab)
    window.async_activate = AsyncMock()
    return window


def test_spawn_endpoint_writes_sidecar(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    repo, name = "r", "wt"
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(
        _isolate["db_path"],
        repo,
        name,
        path=wt_path,
        branch="feature",
        ticket="PROJ-7",
    )

    fake_window = _build_fake_window(
        window_id="W9", claude_session_id="C9", shell_session_id="SH9"
    )
    import iterm2

    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    fake_app = MagicMock()
    fake_app.async_activate = AsyncMock()
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))

    # When the spawn endpoint sends `claude\n` to iTerm2, real Claude
    # would later write a jsonl. We simulate that by patching
    # spawn_two_tab_window to drop a jsonl into the right place
    # immediately. Wrapping the real function:
    real_spawn = iterm_spawn.spawn_two_tab_window

    async def fake_spawn(connection, path, frame):  # type: ignore[no-untyped-def]
        result = await real_spawn(connection, path, frame)
        _make_fake_jsonl(
            _isolate["fake_home"], path, "DISC-UUID-77", mtime=time.time() + 0.01
        )
        return result

    monkeypatch.setattr(iterm_spawn, "spawn_two_tab_window", fake_spawn)
    # Also patch the import site in routes/worktrees.py.
    import app.routes.worktrees as wt_route
    monkeypatch.setattr(wt_route, "spawn_two_tab_window", fake_spawn)

    # Quick timeout so the test polls only briefly when waiting for the
    # background task; the fake jsonl is already on disk at this point.
    monkeypatch.setattr(sidecar, "DEFAULT_POLL_INTERVAL_SECONDS", 0.05)

    # Keep the TestClient context open while we wait — exiting it
    # triggers lifespan shutdown which cancels any in-flight background
    # tasks (including our discovery task) before they can complete.
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(f"/api/worktree/{repo}/{name}/spawn-iterm")
        assert r.status_code == 200, r.text
        body = r.json()
        # claude_session_uuid + sidecar_path are no longer populated
        # inline — discovery + sidecar write run in a background task,
        # so the spawn POST returns instantly.
        assert body["claude_session_uuid"] is None
        assert body["sidecar_path"] is None

        # Wait for the background task to write the sidecar + update
        # the row. Both happen on the event loop in the TestClient's
        # background thread.
        sidecar_path = _wait_for_path(
            _isolate["sidecar_dir"] / "DISC-UUID-77.json", timeout=2.0
        )
        sidecar_data = json.loads(sidecar_path.read_text())
        assert sidecar_data["session_id"] == "DISC-UUID-77"
        assert sidecar_data["started_via"] == "cdh"
        assert sidecar_data["worktree"] == f"{repo}_{name}"
        assert sidecar_data["ticket"] == "PROJ-7"

        uuid = _wait_for_uuid(_isolate["db_path"], repo, name, timeout=2.0)
        assert uuid == "DISC-UUID-77"


def _wait_for_path(p: Path, timeout: float) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p.exists():
            return p
        time.sleep(0.05)
    raise AssertionError(f"path {p} did not appear within {timeout}s")


def _wait_for_uuid(db_path: Path, repo: str, name: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT claude_session_uuid FROM iterm_session "
                "WHERE repo=? AND worktree_name=? AND role='claude'",
                (repo, name),
            ).fetchone()
        finally:
            conn.close()
        if row is not None and row[0] is not None:
            return row[0]
        time.sleep(0.05)
    raise AssertionError(
        f"claude_session_uuid did not appear in DB for {repo}/{name} within {timeout}s"
    )


def test_spawn_endpoint_succeeds_on_discovery_timeout(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    """If Claude doesn't write a jsonl in time, spawn still succeeds but
    claude_session_uuid is null and no sidecar is written. Now that
    discovery runs in a background task this is doubly true — the HTTP
    POST returns instantly regardless of whether Claude's jsonl ever
    appears."""
    repo, name = "r", "wt"
    seed_worktree(
        _isolate["db_path"],
        repo,
        name,
        path=_isolate["dev_root"] / "wt",
        branch="feature",
    )

    fake_window = _build_fake_window()
    import iterm2

    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    fake_app = MagicMock()
    fake_app.async_activate = AsyncMock()
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))
    # Force a quick timeout so the test runs in well under a second.
    monkeypatch.setattr(
        sidecar, "DEFAULT_TIMEOUT_SECONDS", 0.2
    )
    monkeypatch.setattr(
        sidecar, "DEFAULT_POLL_INTERVAL_SECONDS", 0.05
    )

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(f"/api/worktree/{repo}/{name}/spawn-iterm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claude_session_uuid"] is None
    assert body["sidecar_path"] is None
    # No sidecar file written
    assert list(_isolate["sidecar_dir"].glob("*.json")) == []
