"""Ghostty adapter — drives Ghostty.app via its AppleScript dictionary.

Ghostty 1.3.0+ ships an AppleScript surface that lets us:

- Open a new window with an initial working directory + startup command
  (``new surface configuration`` → ``new window with configuration``).
- Open a new tab in a referenced window
  (``new tab in <window> with configuration``).
- Activate an existing window by its AppleScript ``id``.

There is no equivalent of iTerm2's Python API connection — every call
is a one-shot ``osascript`` subprocess. As a result this adapter is
stateless: no supervisor, no persistent connection. The only readiness
check is :func:`is_available`, which probes for ``Ghostty.app`` and
the required version.

Window size is set at spawn time via the ``--window-width`` /
``--window-height`` CLI flags (in cells). AppleScript itself can't
resize windows. Position (x/y) is not exposed at all — Ghostty
inherits OS-default placement.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from app.config.schema import GhosttyWindow
from app.services.terminal import GenericSpawnResult
from app.services.terminal.applescript import (
    build_osascript_args,
    quote,
    shell_single_quote,
)

log = logging.getLogger(__name__)

GHOSTTY_APP_PATH = Path("/Applications/Ghostty.app")
MIN_VERSION = (1, 3, 0)


class GhosttyUnavailable(RuntimeError):
    """Raised when Ghostty.app is missing, too old, or osascript is
    inaccessible. Routes turn this into a 503 with the message."""


# --- availability probing ---------------------------------------------------


_availability_cache: tuple[bool, str | None] | None = None


def is_available() -> tuple[bool, str | None]:
    """Return ``(ok, error_message_or_None)``. Cached for the process
    lifetime — Ghostty's install state doesn't change at runtime in
    any way we'd want to react to."""
    global _availability_cache
    if _availability_cache is not None:
        return _availability_cache

    if not GHOSTTY_APP_PATH.exists():
        _availability_cache = (
            False,
            f"Ghostty.app not found at {GHOSTTY_APP_PATH}. Install Ghostty "
            "(https://ghostty.org) or switch terminal.kind to iterm2.",
        )
        return _availability_cache

    if shutil.which("osascript") is None:
        _availability_cache = (
            False,
            "osascript not found on PATH; required to drive Ghostty.",
        )
        return _availability_cache

    version = _detect_version()
    if version is None:
        # We couldn't parse a version but the app exists. Optimistically
        # assume it'll work; an actual AppleScript call will tell us.
        log.warning("ghostty: could not parse --version output; proceeding anyway")
        _availability_cache = (True, None)
        return _availability_cache

    if version < MIN_VERSION:
        v_str = ".".join(str(p) for p in version)
        min_str = ".".join(str(p) for p in MIN_VERSION)
        _availability_cache = (
            False,
            f"Ghostty {v_str} is too old; CDH needs >= {min_str} for the "
            "AppleScript dictionary.",
        )
        return _availability_cache

    _availability_cache = (True, None)
    return _availability_cache


def _detect_version() -> tuple[int, int, int] | None:
    """Parse ``ghostty --version`` output. Returns ``None`` if the CLI
    is unavailable or the output doesn't parse."""
    cli = shutil.which("ghostty") or "/Applications/Ghostty.app/Contents/MacOS/ghostty"
    if not os.access(cli, os.X_OK):
        return None
    try:
        # Synchronous — only runs at startup / first call; not in a hot path.
        import subprocess

        result = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("ghostty --version probe failed: %s", e)
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def reset_availability_cache() -> None:
    """Test hook — clears the cached probe so the next call re-checks
    the filesystem."""
    global _availability_cache
    _availability_cache = None


def _resolve_claude_cli() -> str:
    """Return an absolute path to the ``claude`` CLI.

    Ghostty's surface ``command`` is exec'd by ``bash --noprofile
    --norc``, so the user's interactive PATH (set in ~/.zshrc etc.) is
    not loaded. A bare ``claude`` invocation hits the minimal PATH
    inherited from launchd and fails with "claude: not found".

    We resolve here, in the backend's process — the CDH backend was
    launched from a user shell, so ``shutil.which`` sees the full PATH
    and finds ``claude`` wherever the user installed it (e.g.
    ``~/.local/bin``). Falls back to bare ``claude`` so the spawn
    still attempts a sensible default if resolution fails; the user
    will see a clear "claude: not found" inside the spawned window.
    """
    return shutil.which("claude") or "claude"


# --- spawn / focus primitives -----------------------------------------------


async def spawn_one_tab_claude(
    cwd: Path, initial_prompt: str, size: GhosttyWindow
) -> None:
    """Open a fresh single-tab Ghostty window at ``cwd`` running
    ``claude '<initial_prompt>'`` as the startup command.

    No tracking is persisted — send-driven spawns are intentionally
    untracked (see PR #108 for rationale).
    """
    _require_available()

    quoted_prompt = shell_single_quote(initial_prompt)
    command = f"{_resolve_claude_cli()} {quoted_prompt}"

    script = [
        'tell application "Ghostty"',
        "  activate",
        "  set cfg to new surface configuration",
        f"  set initial working directory of cfg to {quote(str(cwd))}",
        f"  set command of cfg to {quote(command)}",
        "  new window with configuration cfg",
        "end tell",
    ]
    await _run_osascript(
        script,
        extra_env={
            "CDH_GHOSTTY_W": str(size.width),
            "CDH_GHOSTTY_H": str(size.height),
        },
    )


async def spawn_two_tab_window(
    cwd: Path, size: GhosttyWindow
) -> GenericSpawnResult:
    """Open a fresh Ghostty window with two tabs at ``cwd``: tab 1
    runs ``claude``, tab 2 runs a plain shell. Returns AppleScript
    object ids so the explicit Open flow can persist a row and Focus
    can later target the same window.
    """
    _require_available()

    # Each statement's last expression becomes the script result. We
    # emit a final line that returns a stable, parseable string mixing
    # the window id and both tab ids so we can read all three from one
    # osascript invocation.
    claude_cmd = _resolve_claude_cli()
    script = [
        'tell application "Ghostty"',
        "  activate",
        "  set claudeCfg to new surface configuration",
        f"  set initial working directory of claudeCfg to {quote(str(cwd))}",
        f"  set command of claudeCfg to {quote(claude_cmd)}",
        "  set win to (new window with configuration claudeCfg)",
        "  set claudeTab to item 1 of (tabs of win)",
        "  set shellCfg to new surface configuration",
        f"  set initial working directory of shellCfg to {quote(str(cwd))}",
        "  set shellTab to (new tab in win with configuration shellCfg)",
        '  return ((id of win) as text) & "|" & '
        '((id of claudeTab) as text) & "|" & ((id of shellTab) as text)',
        "end tell",
    ]
    output = await _run_osascript(script)
    parts = output.strip().split("|")
    if len(parts) != 3:
        raise RuntimeError(f"unexpected ghostty spawn output: {output!r}")
    window_id, claude_id, shell_id = parts
    return GenericSpawnResult(
        window_id=window_id,
        claude_session_id=claude_id,
        shell_session_id=shell_id,
        terminal_kind="ghostty",
    )


# --- internals --------------------------------------------------------------


def _require_available() -> None:
    ok, err = is_available()
    if not ok:
        raise GhosttyUnavailable(err or "Ghostty is unavailable")


async def _run_osascript(
    lines: list[str], extra_env: dict[str, str] | None = None
) -> str:
    """Run ``osascript -e <line>…`` and return stdout. Raises if the
    subprocess exits non-zero — the AppleScript error message ends
    up in stderr, which we surface in the exception text.

    ``extra_env`` is currently unused but accepted as a forward-compat
    hook for the day Ghostty exposes window-size knobs through the
    AppleScript dictionary; today the size is configured via the CLI
    spawn path, not the AppleScript spawn path.
    """
    args = ["osascript", *build_osascript_args(lines)]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"osascript exited {proc.returncode}: {stderr_b.decode().strip()}"
        )
    return stdout_b.decode()
