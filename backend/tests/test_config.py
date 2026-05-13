"""Tests for the CDH config schema and loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config.loader import load_config, save_config
from app.config.schema import CDHConfig, RepoConfig


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / "cdh-config.yaml"
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    return config_path


def test_default_config_is_generic() -> None:
    c = CDHConfig()
    assert c.repos == []
    assert c.global_skills == []
    assert c.server.port == 47823
    assert c.server.host == "127.0.0.1"
    assert c.iterm2.default_window.width == 1024
    assert c.iterm2.default_window.height == 768
    assert c.token_monitor.api_url == "http://localhost:47821"
    assert len(c.iterm2.send_gate_patterns) >= 1


def test_global_skill_validates() -> None:
    from app.config.schema import GlobalSkill

    # Happy path
    s = GlobalSkill(name="pr-check-action-required", label="Check action required")
    assert s.cwd == "home"
    assert s.description is None
    # Uppercase / spaces in name → rejected
    with pytest.raises(Exception):
        GlobalSkill(name="UPPERCASE", label="x")
    with pytest.raises(Exception):
        GlobalSkill(name="has spaces", label="x")
    # Empty label → rejected
    with pytest.raises(Exception):
        GlobalSkill(name="ok", label="")
    # Extra keys → rejected (extra="forbid")
    with pytest.raises(Exception):
        GlobalSkill(name="ok", label="x", surprise=True)  # type: ignore[call-arg]


def test_name_must_be_slug() -> None:
    with pytest.raises(Exception):
        RepoConfig(name="UPPERCASE", path=Path("/tmp"))
    with pytest.raises(Exception):
        RepoConfig(name="has spaces", path=Path("/tmp"))
    with pytest.raises(Exception):
        RepoConfig(name="", path=Path("/tmp"))
    r = RepoConfig(name="my-app_2", path=Path("/tmp"))
    assert r.name == "my-app_2"


def test_path_expands_tilde() -> None:
    r = RepoConfig(name="x", path="~/somewhere")  # type: ignore[arg-type]
    assert not str(r.path).startswith("~")
    assert str(r.path).endswith("/somewhere")


def test_path_strips_whitespace() -> None:
    r = RepoConfig(name="x", path="  /tmp/foo  ")  # type: ignore[arg-type]
    assert str(r.path) == "/tmp/foo"
    # Including embedded newlines (the actual bug we hit via a wrapped printf).
    r2 = RepoConfig(name="x", path="/tmp/foo\n  ")  # type: ignore[arg-type]
    assert str(r2.path) == "/tmp/foo"


def test_empty_or_whitespace_only_path_rejected() -> None:
    with pytest.raises(Exception):
        RepoConfig(name="x", path="")  # type: ignore[arg-type]
    with pytest.raises(Exception):
        RepoConfig(name="x", path="   ")  # type: ignore[arg-type]
    with pytest.raises(Exception):
        RepoConfig(name="x", path="\n\t ")  # type: ignore[arg-type]


def test_extra_keys_rejected() -> None:
    with pytest.raises(Exception):
        RepoConfig(name="x", path=Path("/tmp"), unknown_field="huh")  # type: ignore[call-arg]


def test_load_missing_file_returns_defaults() -> None:
    c = load_config()
    assert c.repos == []
    assert c.server.port == 47823


def test_save_load_round_trip() -> None:
    c = CDHConfig()
    c.repos.append(
        RepoConfig(
            name="my-app",
            path=Path("/tmp/app"),
            ticket_pattern=r"PROJ-\d+",
        )
    )
    save_config(c)
    loaded = load_config()
    assert len(loaded.repos) == 1
    assert loaded.repos[0].name == "my-app"
    assert loaded.repos[0].ticket_pattern == r"PROJ-\d+"


def test_save_leaves_no_tempfile(_isolate_config: Path) -> None:
    save_config(CDHConfig())
    leftover = list(_isolate_config.parent.glob(f".{_isolate_config.name}.*.tmp"))
    assert leftover == []
