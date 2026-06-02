"""Tests for the hub-level Claude launcher endpoints (Open Claude + Ask Claude)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import iterm_spawn
from tests.fixtures.config import write_minimal_config
from tests.fixtures.iterm import build_fake_global_window


def _stub_iterm2(monkeypatch: pytest.MonkeyPatch, fake_window: MagicMock) -> None:
    import iterm2

    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    fake_app = MagicMock()
    fake_app.async_activate = AsyncMock()
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))


def test_no_terminal_session_row_written(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    """Global spawns must NOT touch the terminal_session table — those
    rows are FK-bound to a worktree, which a global spawn doesn't have."""
    write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    fake_window = build_fake_global_window()
    _stub_iterm2(monkeypatch, fake_window)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global/open")
    assert r.status_code == 200

    conn = db.open_db(_isolate["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM terminal_session").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# --- direct test of the spawn helper too --------------------------------


def test_spawn_global_claude_window_sends_correct_keystrokes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bypass the HTTP layer — just exercise the helper directly to
    pin the exact keystroke shape."""
    import iterm2

    fake_window = build_fake_global_window(window_id="W7", session_id="S7")
    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    fake_app = MagicMock()
    fake_app.async_activate = AsyncMock()
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))

    from app.config.schema import ITermWindow

    frame = ITermWindow(width=900, height=700, x=10, y=20)
    fake_conn = MagicMock()

    result = asyncio.run(
        iterm_spawn.spawn_global_claude_window(
            fake_conn, tmp_path, frame, "/pr-check-action-required"
        )
    )
    assert result.window_id == "W7"
    assert result.claude_session_id == "S7"

    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    assert sent == f"cd {tmp_path}\nclaude '/pr-check-action-required'\n"
    fake_window.async_set_frame.assert_awaited_once()
    fake_window.async_activate.assert_awaited_once()


def test_spawn_global_claude_window_no_prompt_launches_plain_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``initial_prompt=None`` the helper launches a bare
    ``claude`` (blank session) rather than ``claude '<prompt>'``."""
    import iterm2

    fake_window = build_fake_global_window(window_id="W8", session_id="S8")
    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    fake_app = MagicMock()
    fake_app.async_activate = AsyncMock()
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))

    from app.config.schema import ITermWindow

    frame = ITermWindow(width=900, height=700, x=10, y=20)

    result = asyncio.run(
        iterm_spawn.spawn_global_claude_window(MagicMock(), tmp_path, frame)
    )
    assert result.window_id == "W8"

    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    assert sent == f"cd {tmp_path}\nclaude\n"


# --- blank-session ("Open Claude") endpoint ---------------------------------


def test_open_503_when_iterm_disconnected(_isolate: dict[str, Path]) -> None:
    write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"]
    )
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post("/api/skills/global/open")
    assert r.status_code == 503


def test_open_400_when_development_root_missing(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    bogus = tmp_path / "no-such-dir"
    _isolate["config_path"].write_text(
        yaml.safe_dump(
            {"development_root": str(bogus), "repos": []}
        )
    )
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global/open")
    assert r.status_code == 400
    assert "development_root" in r.json()["detail"]


def test_open_happy_path_spawns_blank_claude_in_dev_root(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a blank ``claude`` session opens in development_root
    with no prompt argument."""
    write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"]
    )
    fake_window = build_fake_global_window(window_id="GW0", session_id="GS0")
    _stub_iterm2(monkeypatch, fake_window)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global/open")

    assert r.status_code == 200, r.text
    assert r.json() == {"spawned": True}

    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    assert sent == f"cd {_isolate['dev_root']}\nclaude\n"


# --- free-form prompt endpoint ----------------------------------------------


def test_freeform_empty_prompt_returns_422(_isolate: dict[str, Path]) -> None:
    write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"]
    )
    with TestClient(app) as client:
        r = client.post("/api/skills/global/freeform", json={"prompt": ""})
    assert r.status_code == 422


def test_freeform_503_when_iterm_disconnected(_isolate: dict[str, Path]) -> None:
    write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"]
    )
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post(
            "/api/skills/global/freeform",
            json={"prompt": "what is the status of PROJ-218?"},
        )
    assert r.status_code == 503


def test_freeform_400_when_development_root_missing(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    bogus = tmp_path / "no-such-dir"
    _isolate["config_path"].write_text(
        yaml.safe_dump({"development_root": str(bogus), "repos": []})
    )
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(
            "/api/skills/global/freeform",
            json={"prompt": "hi"},
        )
    assert r.status_code == 400
    assert "development_root" in r.json()["detail"]


def test_freeform_happy_path_spawns_iterm_with_user_prompt(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the user-typed prompt arrives at iTerm2 as the
    initial argument to ``claude '<prompt>'``."""
    write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"]
    )
    fake_window = build_fake_global_window(window_id="GW9", session_id="GS9")
    _stub_iterm2(monkeypatch, fake_window)

    user_prompt = "summarize what changed in the inbox over the last 24h"
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(
            "/api/skills/global/freeform", json={"prompt": user_prompt}
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"spawned": True}

    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    # The keystroke shape: cd into development_root, then `claude '<prompt>'`.
    assert f"claude '{user_prompt}'" in sent
