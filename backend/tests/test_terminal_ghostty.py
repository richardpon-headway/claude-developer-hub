"""Tests for the Ghostty terminal adapter.

We never invoke a real ``osascript`` in tests — that would actually
open Ghostty windows. Each test patches
``asyncio.create_subprocess_exec`` and asserts:

- The AppleScript we generate has the right shape (correct working
  directory, correct startup command, correct quoting).
- DB writes happen via the same iTerm2 helpers, tagged with
  ``terminal_kind='ghostty'``.
- Availability gating produces the documented exception when
  Ghostty.app is missing or too old.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config.schema import GhosttyWindow
from app.services.terminal import ghostty
from app.services.terminal.applescript import quote, shell_single_quote
from tests.fixtures.worktree import seed_worktree


@pytest.fixture(autouse=True)
def _reset_ghostty_cache() -> None:
    ghostty.reset_availability_cache()
    yield
    ghostty.reset_availability_cache()


# --- applescript quoting ----------------------------------------------------


def test_applescript_quote_handles_specials() -> None:
    assert quote("hello") == '"hello"'
    assert quote('he "said" hi') == r'"he \"said\" hi"'
    assert quote(r"path\with\backslash") == r'"path\\with\\backslash"'
    # Single quotes pass through unchanged (AppleScript treats ' as literal).
    assert quote("it's") == "\"it's\""


def test_shell_single_quote_escapes_single_quotes() -> None:
    assert shell_single_quote("hello") == "'hello'"
    assert shell_single_quote("it's") == "'it'\\''s'"


# --- availability probe -----------------------------------------------------


def test_is_available_when_app_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ghostty, "GHOSTTY_APP_PATH", tmp_path / "does-not-exist.app")
    ok, err = ghostty.is_available()
    assert ok is False
    assert err is not None
    assert "Ghostty.app not found" in err


def _fake_osascript_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which("osascript")`` return a non-None value so
    the availability probe progresses past the "osascript not found"
    gate. Linux CI runners don't have osascript installed; macOS dev
    machines do."""
    import shutil

    real_which = shutil.which

    def fake_which(cmd: str, *args: object, **kwargs: object) -> str | None:
        if cmd == "osascript":
            return "/usr/bin/osascript"
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


def test_is_available_when_version_too_old(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_app = tmp_path / "Ghostty.app"
    fake_app.mkdir()
    monkeypatch.setattr(ghostty, "GHOSTTY_APP_PATH", fake_app)
    monkeypatch.setattr(ghostty, "_detect_version", lambda: (1, 2, 0))
    _fake_osascript_on_path(monkeypatch)
    ok, err = ghostty.is_available()
    assert ok is False
    assert err is not None
    assert "too old" in err.lower()


def test_is_available_when_modern(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_app = tmp_path / "Ghostty.app"
    fake_app.mkdir()
    monkeypatch.setattr(ghostty, "GHOSTTY_APP_PATH", fake_app)
    monkeypatch.setattr(ghostty, "_detect_version", lambda: (1, 3, 0))
    _fake_osascript_on_path(monkeypatch)
    ok, err = ghostty.is_available()
    assert ok is True
    assert err is None


# --- spawn primitives -------------------------------------------------------


def _make_subprocess_mock(stdout: bytes = b"", returncode: int = 0) -> AsyncMock:
    """Build an AsyncMock that returns a fake subprocess whose
    ``communicate`` yields ``(stdout, b"")`` and whose ``returncode``
    matches the requested exit code."""
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(stdout, b""))
    fake_proc.returncode = returncode
    mock = AsyncMock(return_value=fake_proc)
    return mock


def _force_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_app = tmp_path / "Ghostty.app"
    fake_app.mkdir()
    monkeypatch.setattr(ghostty, "GHOSTTY_APP_PATH", fake_app)
    monkeypatch.setattr(ghostty, "_detect_version", lambda: (1, 3, 0))
    _fake_osascript_on_path(monkeypatch)
    ghostty.reset_availability_cache()


def test_spawn_one_tab_claude_emits_expected_applescript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_available(monkeypatch, tmp_path)
    cwd = tmp_path / "my-worktree"
    cwd.mkdir()
    proc_mock = _make_subprocess_mock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", proc_mock)

    asyncio.run(
        ghostty.spawn_one_tab_claude(
            cwd, "/pr-review", GhosttyWindow(width=120, height=40)
        )
    )

    # First positional arg of the subprocess call is always "osascript".
    call_args = proc_mock.await_args
    assert call_args is not None
    args = call_args.args
    assert args[0] == "osascript"

    # Concatenate every -e line into a single script for easier assertions.
    script = "\n".join(arg for arg in args[2:] if arg != "-e")
    assert 'tell application "Ghostty"' in script
    assert "new surface configuration" in script
    assert "set initial working directory of cfg" in script
    assert str(cwd) in script
    # The prompt should be POSIX-single-quoted for `claude '<prompt>'`.
    assert "claude '/pr-review'" in script
    assert "new window with configuration cfg" in script


def test_spawn_one_tab_claude_no_prompt_runs_bare_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``initial_prompt=None`` the startup command is a bare
    ``claude`` — no quoted prompt argument."""
    _force_available(monkeypatch, tmp_path)
    proc_mock = _make_subprocess_mock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", proc_mock)

    asyncio.run(
        ghostty.spawn_one_tab_claude(
            tmp_path, None, GhosttyWindow(width=120, height=40)
        )
    )

    args = proc_mock.await_args.args
    script = "\n".join(arg for arg in args[2:] if arg != "-e")
    # No single-quoted prompt argument is appended.
    assert "claude '" not in script
    assert "set command of cfg" in script


def test_spawn_one_tab_claude_propagates_osascript_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_available(monkeypatch, tmp_path)
    proc_mock = MagicMock()
    proc_mock.communicate = AsyncMock(return_value=(b"", b"AppleScript boom"))
    proc_mock.returncode = 1
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=proc_mock)
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            ghostty.spawn_one_tab_claude(
                tmp_path, "/x", GhosttyWindow(width=80, height=24)
            )
        )
    assert "osascript exited 1" in str(exc.value)
    assert "AppleScript boom" in str(exc.value)


def test_spawn_two_tab_window_parses_returned_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_available(monkeypatch, tmp_path)
    proc_mock = _make_subprocess_mock(stdout=b"WID-7|TAB-A|TAB-B\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", proc_mock)

    result = asyncio.run(
        ghostty.spawn_two_tab_window(tmp_path, GhosttyWindow(width=120, height=40))
    )
    assert result.window_id == "WID-7"
    assert result.claude_session_id == "TAB-A"
    assert result.shell_session_id == "TAB-B"
    assert result.terminal_kind == "ghostty"


def test_spawn_two_tab_window_rejects_malformed_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _force_available(monkeypatch, tmp_path)
    proc_mock = _make_subprocess_mock(stdout=b"only-two|parts\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", proc_mock)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            ghostty.spawn_two_tab_window(tmp_path, GhosttyWindow(width=80, height=24))
        )
    assert "unexpected ghostty spawn output" in str(exc.value)


def test_spawn_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ghostty, "GHOSTTY_APP_PATH", tmp_path / "missing.app")
    ghostty.reset_availability_cache()

    with pytest.raises(ghostty.GhosttyUnavailable):
        asyncio.run(
            ghostty.spawn_one_tab_claude(
                tmp_path, "/x", GhosttyWindow(width=80, height=24)
            )
        )


# --- DB integration: terminal_kind='ghostty' write -------------------------


def test_upsert_records_terminal_kind_ghostty(_isolate: dict[str, Path]) -> None:
    """When the spawn-iterm route persists a Ghostty spawn, the
    terminal_session row records ``terminal_kind='ghostty'``."""
    from app.services.iterm_spawn import SpawnResult, upsert_iterm_sessions_sync

    repo, name = "myapp", "ft"
    seed_worktree(_isolate["db_path"], repo, name, path=_isolate["dev_root"] / "ft")

    result = SpawnResult(
        window_id="GW-1", claude_session_id="GS-A", shell_session_id="GS-B"
    )
    upsert_iterm_sessions_sync(repo, name, result, None, "ghostty")

    conn = sqlite3.connect(_isolate["db_path"])
    try:
        rows = list(
            conn.execute(
                "SELECT role, terminal_kind, window_id, session_id "
                "FROM terminal_session WHERE repo=? AND worktree_name=?",
                (repo, name),
            )
        )
    finally:
        conn.close()
    by_role = {r[0]: r for r in rows}
    assert by_role["claude"] == ("claude", "ghostty", "GW-1", "GS-A")
    assert by_role["shell"] == ("shell", "ghostty", "GW-1", "GS-B")


# --- adapter dispatch -------------------------------------------------------


def test_active_kind_reads_config(_isolate: dict[str, Path]) -> None:
    """``terminal.active_kind()`` returns whatever the user config says."""
    from app.services import terminal as adapter

    _isolate["config_path"].write_text(
        "repos: []\n"
        "terminal:\n"
        "  kind: ghostty\n"
    )
    assert adapter.active_kind() == "ghostty"

    _isolate["config_path"].write_text(
        "repos: []\n"
        "terminal:\n"
        "  kind: iterm2\n"
    )
    assert adapter.active_kind() == "iterm2"


def test_display_name_table() -> None:
    from app.services import terminal as adapter

    assert adapter.display_name("iterm2") == "iTerm2"
    assert adapter.display_name("ghostty") == "Ghostty"
    # Unknown kinds pass through as-is rather than blowing up — adapter
    # selection raises 500 later, this helper just labels.
    assert adapter.display_name("xterm") == "xterm"


def test_unused_patch_silences_lint() -> None:
    """``patch`` is imported for future tests; this no-op keeps lint
    happy until those tests exist."""
    _ = patch
    _ = Any
