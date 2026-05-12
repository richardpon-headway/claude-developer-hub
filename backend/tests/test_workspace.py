"""Tests for /api/workspace/from-path (the cdh-shell-function endpoint)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    db.apply_migrations_sync(db_path)
    return {"db_path": db_path, "tmp_path": tmp_path}


def _seed_worktree(db_path: Path, repo: str, name: str, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
            "VALUES (?, ?, ?, 'main', '2026-01-01T00:00:00Z', 'ready')",
            (repo, name, str(path)),
        )
        conn.commit()
    finally:
        conn.close()


def test_from_path_returns_hub_when_no_match(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.get(
            "/api/workspace/from-path",
            params={"path": str(_isolate["tmp_path"] / "no-such-dir")},
        )
    assert r.status_code == 200
    assert r.json() == {"url": "/"}


def test_from_path_matches_exact_worktree(_isolate: dict[str, Path]) -> None:
    wt_path = _isolate["tmp_path"] / "wt"
    _seed_worktree(_isolate["db_path"], "myrepo", "feature", wt_path)
    with TestClient(app) as client:
        r = client.get(
            "/api/workspace/from-path", params={"path": str(wt_path)}
        )
    assert r.status_code == 200
    assert r.json() == {"url": "/workspace/myrepo/feature"}


def test_from_path_resolves_trailing_slash_and_dots(
    _isolate: dict[str, Path],
) -> None:
    wt_path = _isolate["tmp_path"] / "wt2"
    _seed_worktree(_isolate["db_path"], "myrepo", "feature2", wt_path)
    # Path with trailing slash + roundtrip through .. should still match.
    awkward = f"{wt_path}/."
    with TestClient(app) as client:
        r = client.get("/api/workspace/from-path", params={"path": awkward})
    assert r.status_code == 200
    assert r.json() == {"url": "/workspace/myrepo/feature2"}


def test_from_path_400_when_not_absolute(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.get(
            "/api/workspace/from-path", params={"path": "relative/path"}
        )
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


def test_from_path_400_when_empty(_isolate: dict[str, Path]) -> None:
    # Pydantic's min_length=1 enforces this at validation time.
    with TestClient(app) as client:
        r = client.get("/api/workspace/from-path", params={"path": ""})
    assert r.status_code == 422


def test_from_path_falls_back_when_db_has_other_worktrees(
    _isolate: dict[str, Path],
) -> None:
    """Sanity: a path that doesn't match any seeded worktree falls back
    to the hub URL even when other worktrees exist in the DB."""
    _seed_worktree(_isolate["db_path"], "r", "wt", _isolate["tmp_path"] / "wt")
    with TestClient(app) as client:
        r = client.get(
            "/api/workspace/from-path",
            params={"path": str(_isolate["tmp_path"] / "unrelated")},
        )
    assert r.status_code == 200
    assert r.json() == {"url": "/"}
