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
        return Path(v).expanduser()
    return v


ExpandedPath = Annotated[Path, BeforeValidator(_expand_home)]


class SetupStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str = Field(..., min_length=1)
    cwd: str = ""


class JiraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["acli", "jira-cli", "none"] = "none"
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
    jira: JiraConfig = Field(default_factory=JiraConfig)


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


class CDHConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    development_root: ExpandedPath = Field(
        default_factory=lambda: Path.home() / "development"
    )
    repos: list[RepoConfig] = Field(default_factory=list)
    iterm2: ITermConfig = Field(default_factory=ITermConfig)
    token_monitor: TokenMonitorConfig = Field(default_factory=TokenMonitorConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
