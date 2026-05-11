"""Tests for the /api/repos endpoints (list + Claude-driven onboarding)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config.loader import save_config
from app.config.schema import CDHConfig, RepoConfig
from app.main import app
from app.routes import repos as repos_module


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / "cdh-config.yaml"
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    repos_module._sessions.clear()
    return config_path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_list_repos_initially_empty() -> None:
    with TestClient(app) as client:
        r = client.get("/api/repos")
    assert r.status_code == 200
    assert r.json() == []


def test_onboard_rejects_non_absolute_path() -> None:
    with TestClient(app) as client:
        r = client.post("/api/repos/onboard", json={"path": "relative/path"})
    assert r.status_code == 400


def test_onboard_rejects_non_existent_path(tmp_path: Path) -> None:
    with TestClient(app) as client:
        r = client.post("/api/repos/onboard", json={"path": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_onboard_rejects_non_git_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    with TestClient(app) as client:
        r = client.post("/api/repos/onboard", json={"path": str(plain)})
    assert r.status_code == 400


def test_onboard_returns_session_id_and_prompt(git_repo: Path) -> None:
    with TestClient(app) as client:
        r = client.post("/api/repos/onboard", json={"path": str(git_repo)})
    assert r.status_code == 200
    body = r.json()
    assert len(body["session_id"]) > 10
    assert "Inspect the git repo" in body["prompt"]
    assert str(git_repo) in body["prompt"]
    assert "/api/repos/onboard/complete" in body["prompt"]


def test_onboard_status_after_create(git_repo: Path) -> None:
    with TestClient(app) as client:
        r1 = client.post("/api/repos/onboard", json={"path": str(git_repo)})
        sid = r1.json()["session_id"]
        r2 = client.get(f"/api/repos/onboard/{sid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["state"] == "pending"
    assert body["session_id"] == sid
    assert body["proposed_entry"] is None


def test_onboard_status_unknown_session() -> None:
    with TestClient(app) as client:
        r = client.get("/api/repos/onboard/totally-fake-sid")
    assert r.status_code == 404


def test_onboard_complete_saves_to_config(
    git_repo: Path, _isolate_config: Path
) -> None:
    with TestClient(app) as client:
        r1 = client.post("/api/repos/onboard", json={"path": str(git_repo)})
        sid = r1.json()["session_id"]
        r2 = client.post(
            "/api/repos/onboard/complete",
            json={
                "session_id": sid,
                "proposed_entry": {
                    "name": "my-new-app",
                    "path": str(git_repo),
                },
            },
        )
    assert r2.status_code == 200
    body = r2.json()
    assert body["state"] == "saved"
    assert body["saved_entry"]["name"] == "my-new-app"

    assert _isolate_config.exists()
    raw = yaml.safe_load(_isolate_config.read_text())
    assert len(raw["repos"]) == 1
    assert raw["repos"][0]["name"] == "my-new-app"

    with TestClient(app) as client:
        r3 = client.get("/api/repos")
    assert len(r3.json()) == 1
    assert r3.json()[0]["name"] == "my-new-app"


def test_onboard_complete_name_collision(
    git_repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-repo"
    other.mkdir()
    save_config(CDHConfig(repos=[RepoConfig(name="dup", path=other)]))

    with TestClient(app) as client:
        r1 = client.post("/api/repos/onboard", json={"path": str(git_repo)})
        sid = r1.json()["session_id"]
        r2 = client.post(
            "/api/repos/onboard/complete",
            json={
                "session_id": sid,
                "proposed_entry": {
                    "name": "dup",
                    "path": str(git_repo),
                },
            },
        )
    assert r2.status_code == 409
    assert "name" in r2.json()["detail"].lower()


def test_onboard_complete_unknown_session() -> None:
    with TestClient(app) as client:
        r = client.post(
            "/api/repos/onboard/complete",
            json={
                "session_id": "fake-sid",
                "proposed_entry": {"name": "x", "path": "/tmp"},
            },
        )
    assert r.status_code == 404


def test_onboard_rejects_already_configured_path(git_repo: Path) -> None:
    save_config(CDHConfig(repos=[RepoConfig(name="already", path=git_repo)]))
    with TestClient(app) as client:
        r = client.post("/api/repos/onboard", json={"path": str(git_repo)})
    assert r.status_code == 409


def test_onboard_complete_proposed_entry_validates(git_repo: Path) -> None:
    """Pydantic schema validation should reject malformed proposed_entry."""
    with TestClient(app) as client:
        r1 = client.post("/api/repos/onboard", json={"path": str(git_repo)})
        sid = r1.json()["session_id"]
        r2 = client.post(
            "/api/repos/onboard/complete",
            json={
                "session_id": sid,
                "proposed_entry": {
                    "name": "Has Spaces",
                    "path": str(git_repo),
                },
            },
        )
    assert r2.status_code == 422
