"""YAML config writers for tests.

Two helpers cover every existing test's needs:

- ``write_minimal_config`` — empty ``repos`` list + optional inbox/teams,
  global/workspace skills, send-gate patterns, iTerm2 frame block.
- ``write_repo_config`` — one configured repo (the most common
  end-to-end test shape: create / discover / sync against a real repo
  path on disk).

Callers pass ``dev_root=None`` to skip writing the ``development_root``
key. Test files that don't need a dev_root (pure-unit inbox tests) do.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_ITERM2_BLOCK: dict[str, Any] = {
    "default_window": {"width": 800, "height": 600, "x": 0, "y": 0},
}


def write_minimal_config(
    config_path: Path,
    dev_root: Path | None = None,
    *,
    teams: list[str] | None = None,
    global_skills: list[dict] | None = None,
    workspace_skills: list[dict] | None = None,
    iterm2: dict[str, Any] | None | bool = False,
    repos: list[dict] | None = None,
) -> None:
    """Write a minimal config YAML with an empty (or caller-supplied)
    repos list. Every optional block is omitted from the output when
    its argument is at its default — matches the union of behaviors
    the existing per-file ``_write_*_config`` helpers covered.

    The ``iterm2`` arg accepts three forms:
    - ``False`` (default): no iterm2 block written
    - ``True``: write the default ``ITermWindow`` frame block
    - ``dict``: write the caller's block as-is
    """
    cfg: dict[str, Any] = {"repos": repos or []}
    if dev_root is not None:
        cfg["development_root"] = str(dev_root)
    if iterm2 is True:
        cfg["iterm2"] = dict(_DEFAULT_ITERM2_BLOCK)
    elif isinstance(iterm2, dict):
        cfg["iterm2"] = dict(iterm2)
    if teams is not None:
        cfg["inbox"] = {"teams": teams}
    if global_skills is not None:
        cfg["global_skills"] = global_skills
    if workspace_skills is not None:
        cfg["workspace_skills"] = workspace_skills
    config_path.write_text(yaml.safe_dump(cfg))


def write_repo_config(
    config_path: Path,
    dev_root: Path | None,
    repo_path: Path,
    *,
    name: str = "myapp",
    default_branch: str = "main",
    branch_prefix: str = "",
    setup_steps: list[dict] | None = None,
    ticket_pattern: str | None = None,
    github_repo: str | None = None,
) -> None:
    """Write a config YAML with one configured repo entry. Pass
    ``dev_root=None`` to omit the ``development_root`` key entirely
    (matches tests that exercise inbox endpoints without a configured
    dev_root)."""
    entry: dict[str, Any] = {
        "name": name,
        "path": str(repo_path),
        "default_branch": default_branch,
        "branch_prefix": branch_prefix,
        "setup_steps": setup_steps or [],
        "ticket_pattern": ticket_pattern,
    }
    if github_repo is not None:
        entry["github_repo"] = github_repo
    cfg: dict[str, Any] = {"repos": [entry]}
    if dev_root is not None:
        cfg["development_root"] = str(dev_root)
    config_path.write_text(yaml.safe_dump(cfg))
