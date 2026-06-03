"""Tests for the authored-PR routes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import pr_db
from app.services import worktree as wt_svc
from tests.fixtures.config import write_minimal_config, write_repo_config


@pytest.fixture(autouse=True)
def _stub_user_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route layer reads ``gh_identity.get_user_login`` to scope
    the query. Stub a fixed login so tests don't shell to real gh."""
    from app.routes import authored_prs as authored_route
    from app.services import gh_identity

    async def fake() -> str:
        return "me"

    gh_identity.reset_cache()
    monkeypatch.setattr(gh_identity, "get_user_login", fake)
    monkeypatch.setattr(authored_route, "get_user_login", fake)
    yield
    gh_identity.reset_cache()


# --- POST /api/authored-prs/.../pull-down -------------------------------


def test_pull_down_authored_400_when_repo_not_configured(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post("/api/authored-prs/acme/other/42/pull-down")
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_pull_down_authored_happy_path(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    import subprocess

    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.name", "t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo_path), "branch", "feat/x"], check=True)
    write_repo_config(
        _isolate["config_path"], tmp_path, repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )

    from app.services import pull_down

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/x", "isCrossRepository": False}

    monkeypatch.setattr(pull_down, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post("/api/authored-prs/acme/myapp/42/pull-down")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "myapp"

    # Drain the background setup task before reading the row (plan-67
    # made pull-down return as soon as the setting_up row is inserted).
    import asyncio
    asyncio.run(wt_svc.wait_for_setup_complete())

    # Verify the unified pr row got @me as author_login (resolved via
    # get_user_login at the route layer; the worktree projects it via
    # LEFT JOIN at read time).
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        row = conn.execute(
            "SELECT pr.author_login "
            "FROM worktree w "
            "JOIN pr ON pr.pr_repo = w.pr_repo AND pr.pr_number = w.pr_number "
            "WHERE w.repo = ?",
            ("myapp",),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("me",)


# --- PUT /api/authored-prs/.../notes ------------------------------------


def test_update_notes_endpoint_upserts(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/authored-prs/acme/myapp/42/notes",
            json={"notes": "blocked on PROJ-1"},
        )
    assert r.status_code == 200
    assert r.json()["notes"] == "blocked on PROJ-1"

    pr = pr_db.get_pr_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    )
    assert pr is not None
    assert pr.notes == "blocked on PROJ-1"


def test_update_notes_accepts_empty_string(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/authored-prs/acme/myapp/42/notes",
            json={"notes": ""},
        )
    assert r.status_code == 200
    pr = pr_db.get_pr_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    )
    assert pr is not None
    assert pr.notes == ""


def test_update_notes_no_404_even_when_no_prior_row(
    _isolate: dict[str, Path],
) -> None:
    """Authored rows may not be persisted yet, so any (pr_repo,
    pr_number) is a valid notes target — first call upserts a stub
    row, no 404 path."""
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/authored-prs/never/heard-of-it/12345/notes",
            json={"notes": "first time"},
        )
    assert r.status_code == 200
