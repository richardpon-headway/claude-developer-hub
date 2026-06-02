"""Read and atomically write the CDH user-local config file.

Default location is ``~/.config/cdh/config.yaml``. Tests (and power users) can
override via the ``CDH_CONFIG_PATH`` environment variable or by passing
``path`` directly to ``load_config``/``save_config``.

Saves use the write-to-tempfile-then-rename pattern so a half-written file
never appears at the canonical path if the process dies mid-write.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import yaml

from .schema import CDHConfig

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "cdh" / "config.yaml"

# One-shot dedupe so the deprecation log fires once per process even
# though load_config() runs on every request.
_DEPRECATED_KEYS_WARNED: set[str] = set()


def _resolve_path(path: Path | None) -> Path:
    if path is not None:
        return path
    env_override = os.environ.get("CDH_CONFIG_PATH")
    if env_override:
        return Path(env_override).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> CDHConfig:
    resolved = _resolve_path(path)
    if not resolved.exists():
        return CDHConfig()
    with resolved.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    _warn_deprecated_keys(raw)
    _shim_legacy_iterm2_block(raw)
    _drop_removed_skill_keys(raw)
    return CDHConfig.model_validate(raw)


def _warn_deprecated_keys(raw: dict) -> None:
    """Emit a one-time log line for any deprecated config keys present.

    Kept here (rather than as Pydantic validators) so the message fires
    once per process instead of on every ``load_config`` call.
    """
    # The legacy block lives at top-level ``iterm2:`` *or* under the new
    # canonical ``terminal.iterm2`` location once the shim has run. We
    # check both so the deprecation fires regardless of layout.
    iterm2 = raw.get("iterm2") or (raw.get("terminal") or {}).get("iterm2") or {}
    key = "iterm2.send_gate_patterns"
    if iterm2.get("send_gate_patterns") and key not in _DEPRECATED_KEYS_WARNED:
        _DEPRECATED_KEYS_WARNED.add(key)
        log.warning(
            "config: iterm2.send_gate_patterns is deprecated and ignored. "
            "The send-gate was removed; send-to-Claude now spawns a fresh "
            "window with the prompt as Claude's startup arg. You can delete "
            "this block from your config."
        )


def _drop_removed_skill_keys(raw: dict) -> None:
    """Strip the removed ``global_skills`` / ``workspace_skills`` keys so
    configs written before the skill buttons were removed still load
    under the strict (``extra="forbid"``) schema. A one-time deprecation
    warning fires for each key present so the user knows it's ignored.
    """
    for key in ("global_skills", "workspace_skills"):
        if key in raw:
            del raw[key]
            if key not in _DEPRECATED_KEYS_WARNED:
                _DEPRECATED_KEYS_WARNED.add(key)
                log.warning(
                    "config: `%s` is deprecated and ignored. The skill "
                    "buttons were removed; use the hub's Ask Claude box or "
                    "Open Claude button instead. You can delete this block "
                    "from your config.",
                    key,
                )


def _shim_legacy_iterm2_block(raw: dict) -> None:
    """Lift a pre-PR-#109 top-level ``iterm2:`` block under the new
    canonical ``terminal.iterm2:`` location, in place.

    Pre-PR-#109 configs looked like::

        iterm2:
          default_window: {...}

    Post-PR-#109, the canonical shape is::

        terminal:
          kind: iterm2
          iterm2:
            default_window: {...}

    If a config has only the legacy top-level block, this function
    moves it under ``terminal.iterm2`` and sets ``terminal.kind`` to
    ``iterm2``, then drops the top-level key so the strict schema (no
    extras) still validates. A one-time deprecation warning is logged.

    If the user has *both* a top-level ``iterm2:`` and a ``terminal:``
    block, the explicit ``terminal:`` wins and the top-level key is
    silently dropped after warning — assume the user is mid-migration.
    """
    legacy = raw.get("iterm2")
    if legacy is None:
        return

    key = "top_level_iterm2"
    if key not in _DEPRECATED_KEYS_WARNED:
        _DEPRECATED_KEYS_WARNED.add(key)
        log.warning(
            "config: top-level `iterm2:` block is deprecated; move its "
            "contents under `terminal.iterm2:` and set `terminal.kind: "
            "iterm2`. The block is accepted on load for one release."
        )

    terminal = raw.setdefault("terminal", {})
    terminal.setdefault("kind", "iterm2")
    terminal.setdefault("iterm2", legacy)
    del raw["iterm2"]


def save_config(config: CDHConfig, path: Path | None = None) -> None:
    resolved = _resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    serializable = config.model_dump(mode="json")

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        dir=resolved.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(serializable, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp_path, resolved)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
