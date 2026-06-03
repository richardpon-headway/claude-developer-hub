"""Pydantic models for the CDH user-local config file.

The shape mirrors ``~/.config/cdh/config.yaml`` from the project plan §7. All
defaults here are repo- and user-agnostic — Claude populates the per-user
values during onboarding and writes them back to disk.

``ExpandedPath`` is a ``Path`` that runs ``expanduser()`` on incoming strings,
so a user (or Claude) can write ``~/development`` in YAML without surprise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _expand_home(v: Any) -> Any:
    if isinstance(v, str):
        stripped = v.strip()
        if not stripped:
            raise ValueError("path must not be empty or whitespace-only")
        return Path(stripped).expanduser()
    return v


ExpandedPath = Annotated[Path, BeforeValidator(_expand_home)]


class SetupStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str = Field(..., min_length=1)
    cwd: str = ""


class JiraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["acli", "jira-cli", "none"] = "none"
    base_url: str | None = None
    list_jql: str | None = None


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z0-9_-]+$", min_length=1, max_length=64)
    path: ExpandedPath
    default_branch: str = "main"
    branch_prefix: str = ""
    worktree_path_template: str = "{development_root}/{repo}_worktree_{short}"
    setup_steps: list[SetupStep] = Field(default_factory=list)
    ticket_pattern: str | None = None
    # GitHub ``owner/name`` for this repo, used to match a remote PR to
    # a local checkout (bookmark guard, authored-PR list). Optional:
    # when None, matching falls back to the basename portion of
    # ``pr_repo`` against ``RepoConfig.name``. Onboarding detects and
    # populates this via ``gh repo view --json nameWithOwner``.
    github_repo: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )


class ITermWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(1024, gt=0, le=8192)
    height: int = Field(768, gt=0, le=8192)
    x: int = 50
    y: int = 50


class ITermConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_window: ITermWindow = Field(default_factory=ITermWindow)
    # Deprecated. The keystroke-injection send path was removed; "send to
    # Claude" now spawns a fresh window with ``claude '<prompt>'`` as the
    # startup command, so there is nothing to gate. Accepted on load for
    # back-compat with existing user configs; the loader logs a one-time
    # deprecation warning when present.
    send_gate_patterns: list[str] = Field(default_factory=list)


class GhosttyWindow(BaseModel):
    """Initial window size for Ghostty spawns.

    Ghostty's ``--window-width`` / ``--window-height`` are measured in
    terminal grid cells, not pixels. There is no x/y positioning hook —
    Ghostty doesn't accept window coordinates on macOS, so spawns land
    wherever the OS chooses.
    """
    model_config = ConfigDict(extra="forbid")

    width: int = Field(120, gt=0, le=999)
    height: int = Field(40, gt=0, le=999)


class GhosttyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_window: GhosttyWindow = Field(default_factory=GhosttyWindow)


class TerminalConfig(BaseModel):
    """Which terminal CDH drives, plus terminal-specific tuning.

    The ``kind`` field selects the adapter at startup. Each sub-block
    is read only when its kind is active; the inactive block stays on
    the model so the user can pre-configure both halves without losing
    values when toggling between terminals.
    """
    model_config = ConfigDict(extra="forbid")

    kind: Literal["iterm2", "ghostty"] = "iterm2"
    iterm2: ITermConfig = Field(default_factory=ITermConfig)
    ghostty: GhosttyConfig = Field(default_factory=GhosttyConfig)


class TokenMonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: str = "http://localhost:47821"
    sidecar_dir: ExpandedPath = Field(
        default_factory=lambda: Path.home() / ".cache" / "claude-token-monitor" / "session-meta"
    )


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(47823, gt=0, lt=65536)
    host: str = "127.0.0.1"


class PollingConfig(BaseModel):
    """Tuning knobs for the two long-lived background pollers.

    Defaults are set so an idle CDH with ~10 tracked worktrees uses
    roughly 150 GraphQL/hr — well under GitHub's 5000/hr quota — while
    still feeling reactive enough that the hub reflects new PR state
    within a few minutes.
    """

    model_config = ConfigDict(extra="forbid")

    pr_enrichment_interval_seconds: float = Field(
        600.0,
        gt=0,
        description=(
            "How often the enrichment loop refreshes per-PR state "
            "(review decision, CI status, comment counts) for EVERY "
            "row in the unified `pr` table. Each tick shells one "
            "`gh pr view --json …` per pr row."
        ),
    )
    authored_interval_seconds: float = Field(
        600.0,
        gt=0,
        description=(
            "How often the authored discovery loop runs. Each tick "
            "runs one `gh search prs --author=@me --state=open` and "
            "upserts the results into the unified `pr` table."
        ),
    )


class DiffConfig(BaseModel):
    """Tuning for the file-detail view's unified diff rendering."""

    model_config = ConfigDict(extra="forbid")

    default_context_lines: int = Field(
        25,
        ge=0,
        le=500,
        description=(
            "Lines of unchanged context kept around each diff hunk "
            "in the file detail view. Stretches longer than this "
            "collapse to a 'show N unchanged lines' expander."
        ),
    )
    expand_all_threshold: int = Field(
        200,
        ge=0,
        le=10_000,
        description=(
            "Files with this many lines or fewer render fully "
            "expanded (no collapse chrome). Avoids fiddly UI on "
            "small files."
        ),
    )


class CDHConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    development_root: ExpandedPath = Field(
        default_factory=lambda: Path.home() / "development"
    )
    repos: list[RepoConfig] = Field(default_factory=list)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    terminal: TerminalConfig = Field(default_factory=TerminalConfig)
    token_monitor: TokenMonitorConfig = Field(default_factory=TokenMonitorConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    diff: DiffConfig = Field(default_factory=DiffConfig)
