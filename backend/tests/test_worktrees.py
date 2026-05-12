"""Tests for the worktree CRUD slice (model, service, /api/worktree)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.models.worktree import derive_worktree_name, extract_ticket
from app.services import worktree as svc

# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Each test gets a fresh tmp DB, tmp config, tmp development_root, and a
    cleaned in-memory log buffer. The CDH_DB_PATH / CDH_CONFIG_PATH env
    vars steer the production code paths at the loaded backend."""
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    development_root = tmp_path / "dev"
    development_root.mkdir()

    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))

    # Apply migrations to the fresh tmp DB.
    db.apply_migrations_sync(db_path)

    svc._logs.clear()

    return {"db_path": db_path, "config_path": config_path, "dev_root": development_root}


def _init_git_repo(path: Path) -> None:
    """Init a repo with `main` checked out + a `feature` branch available
    for worktree-add (since git rejects worktree-add on a branch that's
    already checked out elsewhere)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init", "-q"], check=True
    )
    subprocess.run(["git", "-C", str(path), "branch", "feature"], check=True)


def _write_config(
    config_path: Path,
    development_root: Path,
    repo_path: Path,
    name: str = "myapp",
    setup_steps: list[dict] | None = None,
    ticket_pattern: str | None = None,
    branch_prefix: str = "",
) -> None:
    config = {
        "development_root": str(development_root),
        "repos": [
            {
                "name": name,
                "path": str(repo_path),
                "default_branch": "main",
                "branch_prefix": branch_prefix,
                "setup_steps": setup_steps or [],
                "ticket_pattern": ticket_pattern,
                "jira": {"tool": "none"},
            }
        ],
    }
    config_path.write_text(yaml.safe_dump(config))




# --- pure helpers --------------------------------------------------------


def test_derive_worktree_name_basic() -> None:
    assert derive_worktree_name("main") == "main"
    assert derive_worktree_name("cleanup-old-foo") == "cleanup_old_foo"


def test_derive_worktree_name_strips_prefix() -> None:
    assert derive_worktree_name("alice/cleanup-foo", "alice/") == "cleanup_foo"


def test_derive_worktree_name_preserves_ticket() -> None:
    out = derive_worktree_name(
        "alice/TICKET-77_login-flow-fix",
        branch_prefix="alice/",
        ticket_pattern=r"[A-Z]+-\d+",
    )
    assert out == "TICKET-77_login_flow_fix"


def test_extract_ticket() -> None:
    assert extract_ticket("alice/PROJ-12_x", r"[A-Z]+-\d+") == "PROJ-12"
    assert extract_ticket("alice/foo", r"[A-Z]+-\d+") is None
    assert extract_ticket("alice/foo", None) is None


# --- /api/worktree -------------------------------------------------------


def test_list_worktrees_initially_empty() -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktrees")
    assert r.status_code == 200
    assert r.json() == []


def test_create_unknown_repo_400(_isolate: dict[str, Path]) -> None:
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        _isolate["dev_root"] / "ignored",
        "registered",
    )
    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "not-registered", "branch": "main"})
    assert r.status_code == 400
    assert "unknown repo" in r.json()["detail"]


def test_create_happy_path(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "echo setup-ran", "cwd": ""}],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["name"] == "feature"
        assert body["branch"] == "feature"
        assert Path(body["path"]).exists()

        r2 = client.get("/api/worktree/myapp/feature")
    detail = r2.json()
    assert detail["row"]["status"] == "ready"
    assert any("setup-ran" in line for line in detail["log"])


def test_create_duplicate_409(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    _write_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r1 = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
        assert r1.status_code == 200
        assert r1.json()["status"] == "ready"
        r2 = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_setup_step_failure_marks_failed(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[
            {"cmd": "echo first-step-ok", "cwd": ""},
            {"cmd": "false", "cwd": ""},  # exit 1
            {"cmd": "echo should-not-run", "cwd": ""},
        ],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
        assert r.status_code == 200
        assert r.json()["status"] == "failed"
        r2 = client.get("/api/worktree/myapp/feature")
    detail = r2.json()
    log = detail["log"]
    assert any("first-step-ok" in line for line in log)
    assert any("setup step 1 failed" in line for line in log)
    assert not any("should-not-run" in line for line in log)


def test_missing_branch_marks_failed(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    _write_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "nope-not-real"})
        assert r.status_code == 200
        assert r.json()["status"] == "failed"
        r2 = client.get("/api/worktree/myapp/nope_not_real")
    detail = r2.json()
    assert any("not found locally or on origin" in line for line in detail["log"])


def test_get_worktree_404() -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/missing")
    assert r.status_code == 404
