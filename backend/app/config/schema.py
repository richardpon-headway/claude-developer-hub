"""Pydantic models for the CDH user-local config file.

The shape mirrors ``~/.config/cdh/config.yaml`` from the project plan §7. All
defaults here are repo- and user-agnostic — Claude populates the per-user
values during onboarding and writes them back to disk.

``ExpandedPath`` is a ``Path`` that runs ``expanduser()`` on incoming strings,
so a user (or Claude) can write ``~/development`` in YAML without surprise.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


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


class GlobalSkill(BaseModel):
    """A Claude slash-command surfaced on the hub page (not bound to a
    worktree). Clicking the button opens a fresh iTerm2 window at
    ``cwd`` and launches ``claude /<name>`` as the initial prompt.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]*$", min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=64)
    description: str | None = None
    # "home" → Path.home() at spawn time. Anything else is treated as a
    # path (tildes expanded, must be absolute + exist when the button is
    # clicked).
    cwd: str = "home"


class WorkspaceSkill(BaseModel):
    """A Claude slash-command surfaced as a button on the workspace
    detail page. Always runs in the worktree's path — no ``cwd`` field,
    unlike :class:`GlobalSkill`. The set of allowed names is the
    server-side allow-list enforced by
    ``POST /api/worktree/{repo}/{name}/run-skill``.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]*$", min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=64)
    description: str | None = None


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z0-9_-]+$", min_length=1, max_length=64)
    path: ExpandedPath
    default_branch: str = "main"
    branch_prefix: str = ""
    worktree_path_template: str = "{development_root}/{repo}_worktree_{short}"
    setup_steps: list[SetupStep] = Field(default_factory=list)
    ticket_pattern: str | None = None
    # GitHub ``owner/name`` for this repo, used by the inbox to match a
    # remote PR to a local checkout. Optional: when None, the inbox
    # falls back to matching the basename portion of ``pr_repo`` against
    # ``RepoConfig.name``. Onboarding (slice 3) will detect and populate
    # this via ``gh repo view --json nameWithOwner``.
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
    send_gate_patterns: list[str] = Field(
        default_factory=lambda: [
            r"Allow .* \[y/N\]\??$",
            r"\? \(y/n\) $",
            r"Press any key to continue",
        ]
    )


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


class InboxConfig(BaseModel):
    """GitHub teams whose review-requested PRs should surface in the
    hub's Inbox section alongside the user's authored + directly-
    review-requested PRs. ``team-review-requested:<owner>/<slug>`` is
    the GitHub search qualifier we drive."""

    model_config = ConfigDict(extra="forbid")

    teams: list[str] = Field(
        default_factory=list,
        description="GitHub team slugs in 'owner/team' form.",
    )

    @field_validator("teams")
    @classmethod
    def _validate_team_slugs(cls, v: list[str]) -> list[str]:
        # Each entry must be ``owner/team-slug``. GitHub allows
        # alphanumerics, underscore, dot, hyphen in both halves.
        pattern = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
        for slug in v:
            if not pattern.match(slug):
                raise ValueError(
                    f"inbox.teams entry must be 'owner/team' "
                    f"(alphanumerics + _.- only); got {slug!r}"
                )
        return v


class CDHConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    development_root: ExpandedPath = Field(
        default_factory=lambda: Path.home() / "development"
    )
    repos: list[RepoConfig] = Field(default_factory=list)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    global_skills: list[GlobalSkill] = Field(default_factory=list)
    workspace_skills: list[WorkspaceSkill] = Field(default_factory=list)
    iterm2: ITermConfig = Field(default_factory=ITermConfig)
    token_monitor: TokenMonitorConfig = Field(default_factory=TokenMonitorConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
