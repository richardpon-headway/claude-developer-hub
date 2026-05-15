"""Tests for the hub-level global-skill spawn endpoint."""
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


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    return {"db_path": db_path, "config_path": config_path, "tmp": tmp_path}


def _write_config(config_path: Path, global_skills: list[dict]) -> None:
    config_path.write_text(
        yaml.safe_dump(
            {
                "development_root": str(config_path.parent),
                "repos": [],
                "global_skills": global_skills,
            }
        )
    )


def _build_fake_window(window_id: str = "GW1", session_id: str = "GS1") -> MagicMock:
    """Single-tab fake — mirrors the worktree-spawn fake but skips the
    second-tab plumbing since global spawns are one-tab only."""
    session = MagicMock(session_id=session_id)
    session.async_send_text = AsyncMock()
    tab = MagicMock(current_session=session)
    tab.async_select = AsyncMock()
    window = MagicMock(window_id=window_id, current_tab=tab)
    window.async_set_frame = AsyncMock()
    window.async_activate = AsyncMock()
    return window


def _stub_iterm2(monkeypatch: pytest.MonkeyPatch, fake_window: MagicMock) -> None:
    import iterm2

    monkeypatch.setattr(
        iterm2.Window, "async_create", AsyncMock(return_value=fake_window)
    )
    fake_app = MagicMock()
    fake_app.async_activate = AsyncMock()
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))


def test_unknown_skill_returns_404(_isolate: dict[str, Path]) -> None:
    _write_config(_isolate["config_path"], [])
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global", json={"skill": "pr-check-action-required"})
    assert r.status_code == 404
    assert "unknown global skill" in r.json()["detail"]


def test_bad_skill_name_format_returns_422(_isolate: dict[str, Path]) -> None:
    _write_config(_isolate["config_path"], [])
    with TestClient(app) as client:
        r = client.post("/api/skills/global", json={"skill": ""})
    assert r.status_code == 422


def test_iterm2_disconnected_returns_503(_isolate: dict[str, Path]) -> None:
    _write_config(
        _isolate["config_path"],
        [{"name": "pr-check-action-required", "label": "Check action required"}],
    )
    with TestClient(app) as client:
        # explicitly no iterm connection
        client.app.state.iterm = None
        r = client.post("/api/skills/global", json={"skill": "pr-check-action-required"})
    assert r.status_code == 503
    assert "iTerm2 not connected" in r.json()["detail"]


def test_happy_path_spawns_in_home(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_config(
        _isolate["config_path"],
        [{"name": "pr-check-action-required", "label": "Check action required"}],
    )
    fake_window = _build_fake_window(window_id="GW42", session_id="GS42")
    _stub_iterm2(monkeypatch, fake_window)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global", json={"skill": "pr-check-action-required"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"window_id": "GW42", "claude_session_id": "GS42"}

    # The shell command must cd to $HOME and launch Claude with the
    # slash command as initial prompt.
    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    assert f"cd {Path.home()}" in sent
    assert "claude '/pr-check-action-required'" in sent

    # Activate path ran so the window comes to the front.
    fake_window.async_activate.assert_awaited_once()
    fake_window.current_tab.async_select.assert_awaited_once()


def test_custom_cwd_resolves(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    target = _isolate["tmp"] / "work"
    target.mkdir()
    _write_config(
        _isolate["config_path"],
        [
            {
                "name": "pr-check-action-required",
                "label": "Check action required",
                "cwd": str(target),
            }
        ],
    )
    fake_window = _build_fake_window()
    _stub_iterm2(monkeypatch, fake_window)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global", json={"skill": "pr-check-action-required"})

    assert r.status_code == 200, r.text
    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    assert f"cd {target}" in sent


def test_custom_cwd_must_exist(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    bogus = _isolate["tmp"] / "does-not-exist"
    _write_config(
        _isolate["config_path"],
        [
            {
                "name": "pr-check-action-required",
                "label": "Check action required",
                "cwd": str(bogus),
            }
        ],
    )
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global", json={"skill": "pr-check-action-required"})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_no_iterm_session_row_written(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    """Global spawns must NOT touch the iterm_session table — those rows
    are FK-bound to a worktree, which a global spawn doesn't have."""
    _write_config(
        _isolate["config_path"],
        [{"name": "pr-check-action-required", "label": "Check action required"}],
    )
    fake_window = _build_fake_window()
    _stub_iterm2(monkeypatch, fake_window)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/skills/global", json={"skill": "pr-check-action-required"})
    assert r.status_code == 200

    conn = db.open_db(_isolate["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) FROM iterm_session").fetchone()[0]
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

    fake_window = _build_fake_window(window_id="W7", session_id="S7")
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


# --- free-form prompt endpoint ----------------------------------------------


def test_freeform_empty_prompt_returns_422(_isolate: dict[str, Path]) -> None:
    _write_config(_isolate["config_path"], [])
    with TestClient(app) as client:
        r = client.post("/api/skills/global/freeform", json={"prompt": ""})
    assert r.status_code == 422


def test_freeform_503_when_iterm_disconnected(_isolate: dict[str, Path]) -> None:
    _write_config(_isolate["config_path"], [])
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
        yaml.safe_dump(
            {
                "development_root": str(bogus),
                "repos": [],
                "global_skills": [],
            }
        )
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
    _write_config(_isolate["config_path"], [])
    fake_window = _build_fake_window(window_id="GW9", session_id="GS9")
    _stub_iterm2(monkeypatch, fake_window)

    user_prompt = "summarize what changed in the inbox over the last 24h"
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(
            "/api/skills/global/freeform", json={"prompt": user_prompt}
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_id"] == "GW9"
    assert body["claude_session_id"] == "GS9"

    sent = fake_window.current_tab.current_session.async_send_text.await_args.args[0]
    # The keystroke shape: cd into development_root, then `claude '<prompt>'`.
    assert f"claude '{user_prompt}'" in sent
