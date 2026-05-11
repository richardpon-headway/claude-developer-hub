"""Read and atomically write the CDH user-local config file.

Default location is ``~/.config/cdh/config.yaml``. Tests (and power users) can
override via the ``CDH_CONFIG_PATH`` environment variable or by passing
``path`` directly to ``load_config``/``save_config``.

Saves use the write-to-tempfile-then-rename pattern so a half-written file
never appears at the canonical path if the process dies mid-write.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from .schema import CDHConfig

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "cdh" / "config.yaml"


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
    return CDHConfig.model_validate(raw)


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
