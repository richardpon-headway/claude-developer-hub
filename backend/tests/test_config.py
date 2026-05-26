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
    assert c.workspace_skills == []
    assert c.server.port == 47823
    assert c.server.host == "127.0.0.1"
    assert c.iterm2.default_window.width == 1024
    assert c.iterm2.default_window.height == 768
    assert c.token_monitor.api_url == "http://localhost:47821"
    # send_gate_patterns is deprecated; default is an empty list. The
    # field is kept on the schema only so existing user configs with
    # the legacy block don't error on load.
    assert c.iterm2.send_gate_patterns == []


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


def test_workspace_skill_validates() -> None:
    from app.config.schema import WorkspaceSkill

    # Happy path — no cwd field, that's the distinction from GlobalSkill.
    s = WorkspaceSkill(name="pr-finalize-for-review", label="/pr-finalize-for-review")
    assert s.description is None
    # Same name regex as GlobalSkill
    with pytest.raises(Exception):
        WorkspaceSkill(name="UPPERCASE", label="x")
    with pytest.raises(Exception):
        WorkspaceSkill(name="has spaces", label="x")
    with pytest.raises(Exception):
        WorkspaceSkill(name="ok", label="")
    # cwd is NOT a WorkspaceSkill field — extra="forbid" should catch attempts
    # to set one (would mean the caller has the wrong model).
    with pytest.raises(Exception):
        WorkspaceSkill(name="ok", label="x", cwd="home")  # type: ignore[call-arg]


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


# --- polling config ---------------------------------------------------------


def test_polling_config_defaults() -> None:
    cfg = CDHConfig()
    assert cfg.polling.pr_state_interval_seconds == 600.0
    assert cfg.polling.inbox_interval_seconds == 300.0


def test_polling_config_accepts_custom_values() -> None:
    cfg = CDHConfig(
        polling={  # type: ignore[arg-type]
            "pr_state_interval_seconds": 1800,
            "inbox_interval_seconds": 900,
        }
    )
    assert cfg.polling.pr_state_interval_seconds == 1800.0
    assert cfg.polling.inbox_interval_seconds == 900.0


@pytest.mark.parametrize(
    "field",
    ["pr_state_interval_seconds", "inbox_interval_seconds"],
)
@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_polling_config_rejects_non_positive(field: str, bad: float) -> None:
    with pytest.raises(Exception) as exc_info:
        CDHConfig(polling={field: bad})  # type: ignore[arg-type]
    assert "greater than 0" in str(exc_info.value).lower()


# --- deprecated send_gate_patterns soft-shim --------------------------------


def test_legacy_send_gate_patterns_load_without_error(
    _isolate_config: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Beta-tester configs written before PR-#1 still have a populated
    ``iterm2.send_gate_patterns`` block. The loader must accept the
    field (it stays on the schema as a soft shim) and emit a one-time
    deprecation log so the user knows the value is now ignored."""
    import yaml

    from app.config import loader as loader_module

    _isolate_config.write_text(
        yaml.safe_dump(
            {
                "repos": [],
                "iterm2": {
                    "default_window": {"width": 1200, "height": 800, "x": 0, "y": 0},
                    "send_gate_patterns": [r"Allow .* \[y/N\]\??$"],
                },
            }
        )
    )

    # Reset the per-process dedupe set so we can observe the warning.
    loader_module._DEPRECATED_KEYS_WARNED.clear()

    with caplog.at_level("WARNING", logger="app.config.loader"):
        c = load_config()

    assert c.iterm2.default_window.width == 1200
    assert any(
        "send_gate_patterns is deprecated" in rec.message for rec in caplog.records
    )
