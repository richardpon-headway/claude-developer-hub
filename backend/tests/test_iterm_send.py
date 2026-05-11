"""Tests for the skill-runner / send-text path: send_to_session + the P0
send-gate + the worktree endpoints. The ``iterm2`` module is mocked at
function level — no live iTerm2 needed."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import iterm_send
from app.services.iterm_send import (
    SendGateError,
    SessionNotFoundError,
    _extract_trailing_text,
    _send_gate_check,
    find_session_by_id,
    send_to_session,
)
from app.services import iterm_spawn


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
    return {"db_path": db_path, "config_path": config_path, "dev_root": dev_root}


def _write_minimal_config(
    config_path: Path,
    dev_root: Path,
    send_gate_patterns: list[str] | None = None,
) -> None:
    iterm2_block: dict = {"default_window": {"width": 800, "height": 600, "x": 0, "y": 0}}
    if send_gate_patterns is not None:
        iterm2_block["send_gate_patterns"] = send_gate_patterns
    config_path.write_text(
        yaml.safe_dump(
            {"development_root": str(dev_root), "repos": [], "iterm2": iterm2_block}
        )
    )


def _seed_claude_session(
    db_path: Path,
    repo: str,
    name: str,
    dev_root: Path,
    claude_sid: str = "S-claude",
) -> None:
    worktree_path = dev_root / name
    worktree_path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo, name, str(worktree_path), "main", "2026-01-01T00:00:00Z", "ready"),
        )
        conn.execute(
            "INSERT INTO iterm_session "
            "(repo, worktree_name, role, iterm_window_id, iterm_session_id, spawned_at) "
            "VALUES (?, ?, 'claude', 'W1', ?, '2026-01-01T00:00:00Z')",
            (repo, name, claude_sid),
        )
        conn.commit()
    finally:
        conn.close()


def _build_screen_contents(trailing_text: str, prefix_lines: int = 3) -> MagicMock:
    """Build a fake ScreenContents that exposes number_of_lines + line(i).
    The last line is ``trailing_text``; preceding lines are blank."""
    total = prefix_lines + 1
    contents = MagicMock(number_of_lines=total)

    def _line(i: int) -> MagicMock:
        return MagicMock(string=trailing_text if i == total - 1 else "")

    contents.line = _line
    return contents


def _build_fake_session(
    session_id: str = "S-claude", trailing_text: str = ""
) -> MagicMock:
    session = MagicMock(session_id=session_id)
    session.async_send_text = AsyncMock()
    session.async_get_screen_contents = AsyncMock(
        return_value=_build_screen_contents(trailing_text)
    )
    return session


def _patch_iterm_app(monkeypatch: pytest.MonkeyPatch, sessions: list[MagicMock]) -> None:
    """Wire the iterm2 module so async_get_app returns an app whose
    one window's one tab contains the given sessions."""
    import iterm2

    tab = MagicMock(sessions=sessions)
    window = MagicMock(tabs=[tab])
    fake_app = MagicMock(windows=[window])
    monkeypatch.setattr(iterm2, "async_get_app", AsyncMock(return_value=fake_app))


# --- pure helpers --------------------------------------------------------


def test_extract_trailing_text_returns_last_nonblank() -> None:
    assert _extract_trailing_text(["a", "b  ", "", "  "]) == "b"
    assert _extract_trailing_text([""]) == ""
    assert _extract_trailing_text([]) == ""
    assert _extract_trailing_text(["only line  "]) == "only line"


def test_send_gate_check_matches_first_pattern() -> None:
    patterns = [r"Allow .* \[y/N\]\??$", r"\? \(y/n\) $"]
    assert _send_gate_check("Allow Bash? [y/N]", patterns) == r"Allow .* \[y/N\]\??$"
    assert _send_gate_check("Continue? (y/n) ", patterns) == r"\? \(y/n\) $"
    assert _send_gate_check("some random text", patterns) is None


def test_send_gate_check_skips_invalid_regex() -> None:
    # Malformed pattern is logged + skipped; a valid one still matches.
    patterns = [r"[unbalanced", r"Allow .* \[y/N\]\??$"]
    assert _send_gate_check("Allow Bash? [y/N]", patterns) == r"Allow .* \[y/N\]\??$"


# --- find_session_by_id --------------------------------------------------


@pytest.mark.asyncio
async def test_find_session_by_id_walks_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = MagicMock(session_id="target")
    other = MagicMock(session_id="other")
    _patch_iterm_app(monkeypatch, [other, target])

    found = await find_session_by_id(MagicMock(), "target")
    assert found is target

    not_found = await find_session_by_id(MagicMock(), "nope")
    assert not_found is None


# --- send_to_session -----------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_session_sends_with_newline_by_default(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"], send_gate_patterns=[])
    session = _build_fake_session(session_id="X")
    _patch_iterm_app(monkeypatch, [session])

    await send_to_session(MagicMock(), "X", "/pr-check-action-required")
    session.async_send_text.assert_awaited_once_with("/pr-check-action-required\n")


@pytest.mark.asyncio
async def test_send_to_session_no_newline_when_disabled(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"], send_gate_patterns=[])
    session = _build_fake_session(session_id="X")
    _patch_iterm_app(monkeypatch, [session])

    await send_to_session(MagicMock(), "X", "partial", press_enter=False)
    session.async_send_text.assert_awaited_once_with("partial")


@pytest.mark.asyncio
async def test_send_to_session_refuses_on_gate_match(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        send_gate_patterns=[r"Allow .* \[y/N\]\??$"],
    )
    session = _build_fake_session(session_id="X", trailing_text="Allow Bash command? [y/N]")
    _patch_iterm_app(monkeypatch, [session])

    with pytest.raises(SendGateError) as exc_info:
        await send_to_session(MagicMock(), "X", "/skill")
    assert exc_info.value.matched_pattern == r"Allow .* \[y/N\]\??$"
    assert "Allow Bash" in exc_info.value.trailing
    session.async_send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_to_session_gate_can_be_bypassed(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    """`send_gate=False` skips the gate. Used by tests + by a future
    'force send' UI affordance."""
    _write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        send_gate_patterns=[r"."],  # matches anything
    )
    session = _build_fake_session(session_id="X", trailing_text="anything")
    _patch_iterm_app(monkeypatch, [session])

    await send_to_session(MagicMock(), "X", "force me", send_gate=False)
    session.async_send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_to_session_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_iterm_app(monkeypatch, [_build_fake_session(session_id="other")])
    with pytest.raises(SessionNotFoundError):
        await send_to_session(MagicMock(), "missing", "x")


# --- /api/worktree/{repo}/{name}/send-text endpoint ----------------------


def test_send_text_503_when_disconnected(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    _seed_claude_session(_isolate["db_path"], "r", "wt", _isolate["dev_root"])
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post("/api/worktree/r/wt/send-text", json={"text": "hi"})
    assert r.status_code == 503


def test_send_text_400_when_no_claude_session(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    # Worktree exists but never had spawn-iterm called for it.
    conn = sqlite3.connect(_isolate["db_path"])
    conn.execute(
        "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
        "VALUES ('r', 'wt', '/tmp', 'main', '2026', 'ready')"
    )
    conn.commit()
    conn.close()

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/worktree/r/wt/send-text", json={"text": "hi"})
    assert r.status_code == 400
    assert "open it in iTerm2" in r.json()["detail"]


def test_send_text_404_when_session_vanished(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    _seed_claude_session(_isolate["db_path"], "r", "wt", _isolate["dev_root"], "S-stale")
    # iTerm2 has no sessions matching that id (user closed the window).
    _patch_iterm_app(monkeypatch, [_build_fake_session("S-someone-else")])

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/worktree/r/wt/send-text", json={"text": "hi"})
    assert r.status_code == 404


def test_send_text_409_on_send_gate(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        send_gate_patterns=[r"Allow .* \[y/N\]\??$"],
    )
    _seed_claude_session(_isolate["db_path"], "r", "wt", _isolate["dev_root"], "S-claude")
    session = _build_fake_session("S-claude", trailing_text="Allow git push? [y/N]")
    _patch_iterm_app(monkeypatch, [session])

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post("/api/worktree/r/wt/send-text", json={"text": "y"})
    assert r.status_code == 409
    assert "awaiting input" in r.json()["detail"]
    session.async_send_text.assert_not_awaited()


def test_send_text_happy_path(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"], send_gate_patterns=[]
    )
    _seed_claude_session(_isolate["db_path"], "r", "wt", _isolate["dev_root"], "S-claude")
    session = _build_fake_session("S-claude")
    _patch_iterm_app(monkeypatch, [session])

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(
            "/api/worktree/r/wt/send-text",
            json={"text": "look at PROJ-12", "press_enter": True},
        )
    assert r.status_code == 200
    assert r.json() == {"sent": True}
    session.async_send_text.assert_awaited_once_with("look at PROJ-12\n")


# --- /api/worktree/{repo}/{name}/run-skill endpoint ----------------------


def test_run_skill_validates_name() -> None:
    """Pydantic should reject invalid skill names (spaces, uppercase, slashes)."""
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        for bad in ["has spaces", "UPPER", "with/slash", ""]:
            r = client.post(
                "/api/worktree/r/wt/run-skill", json={"skill_name": bad}
            )
            assert r.status_code == 422, f"expected 422 for {bad!r} got {r.status_code}"


def test_run_skill_prefixes_with_slash(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _write_minimal_config(
        _isolate["config_path"], _isolate["dev_root"], send_gate_patterns=[]
    )
    _seed_claude_session(_isolate["db_path"], "r", "wt", _isolate["dev_root"], "S-claude")
    session = _build_fake_session("S-claude")
    _patch_iterm_app(monkeypatch, [session])

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=MagicMock())
        r = client.post(
            "/api/worktree/r/wt/run-skill",
            json={"skill_name": "pr-check-action-required"},
        )
    assert r.status_code == 200
    session.async_send_text.assert_awaited_once_with("/pr-check-action-required\n")
