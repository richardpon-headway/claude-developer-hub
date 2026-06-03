"""Tests for the bookmark routes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import pr_db
from app.services import worktree as wt_svc
from tests.fixtures.bookmark import seed_bookmark
from tests.fixtures.config import write_minimal_config, write_repo_config


def _config_with_acme_myapp(config_path: Path) -> None:
    """Write a config with ``acme/myapp`` configured so the bookmark
    repo-configured guard passes. The guard is a pure config lookup —
    the path needn't exist on disk for tests that mock ``gh``."""
    write_minimal_config(
        config_path,
        repos=[
            {
                "name": "myapp",
                "path": "/tmp/cdh-myapp-cfg",
                "github_repo": "acme/myapp",
            }
        ],
    )


# --- POST /api/bookmarks (add) -----------------------------------------


def test_add_bookmark_400_on_bad_url(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post("/api/bookmarks", json={"url": "not a url"})
    assert r.status_code == 400
    assert "github.com" in r.json()["detail"].lower()


def test_add_bookmark_400_when_repo_not_configured(
    _isolate: dict[str, Path],
) -> None:
    """A PR from a repo that isn't in the REPOS list can't be
    bookmarked — the user must add the repo first."""
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/42"},
        )
    assert r.status_code == 400
    assert "add a repo" in r.json()["detail"].lower()


def test_add_bookmark_happy_path(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_with_acme_myapp(_isolate["config_path"])

    from app.routes import bookmarks as bookmarks_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {
            "title": "fix the thing",
            "author": {"login": "alice"},
            "url": "https://github.com/acme/myapp/pull/42",
            "state": "OPEN",
        }

    monkeypatch.setattr(bookmarks_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/42"},
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["pr_repo"] == "acme/myapp"
    assert body["pr_number"] == 42
    assert body["title"] == "fix the thing"
    assert body["author_login"] == "alice"
    assert body["state"] == "open"


def test_add_bookmark_extracts_ticket_via_configured_pattern(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"], tmp_path, repo_path,
        name="myapp",
        ticket_pattern=r"[A-Z]+-\d+",
    )

    from app.routes import bookmarks as bookmarks_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {
            "title": "PROJ-218: do the thing",
            "author": {"login": "alice"},
            "url": "https://github.com/acme/myapp/pull/42",
            "state": "OPEN",
        }

    monkeypatch.setattr(bookmarks_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/42"},
        )
    assert r.status_code == 201, r.text
    assert r.json()["ticket"] == "PROJ-218"


def test_add_bookmark_409_when_already_exists(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_with_acme_myapp(_isolate["config_path"])
    seed_bookmark(_isolate["db_path"], pr_repo="acme/myapp", pr_number=42)

    from app.routes import bookmarks as bookmarks_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {
            "title": "fix",
            "author": {"login": "alice"},
            "url": "https://github.com/acme/myapp/pull/42",
            "state": "OPEN",
        }

    monkeypatch.setattr(bookmarks_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/42"},
        )
    assert r.status_code == 409


def test_add_bookmark_404_when_pr_missing(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_with_acme_myapp(_isolate["config_path"])

    from app.routes import bookmarks as bookmarks_route
    from app.services.gh_cli import GhFailed

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        raise GhFailed(
            ["pr", "view"],
            "GraphQL: Could not resolve to a PullRequest with the number of 999",
        )

    monkeypatch.setattr(bookmarks_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/999"},
        )
    assert r.status_code == 404


def test_add_bookmark_accepts_url_with_trailing_path(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """URLs copied from /files, /commits, etc. should still parse."""
    _config_with_acme_myapp(_isolate["config_path"])

    from app.routes import bookmarks as bookmarks_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {
            "title": "fix",
            "author": {"login": "alice"},
            "url": "https://github.com/acme/myapp/pull/42",
            "state": "OPEN",
        }

    monkeypatch.setattr(bookmarks_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/42/files"},
        )
    assert r.status_code == 201, r.text


def test_add_bookmark_layers_onto_existing_unbookmarked_row(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pr row that exists via another surface (e.g. authored-tier
    notes) can still be bookmarked — only the duplicate ``is_bookmarked``
    case triggers 409. Existing notes survive via COALESCE."""
    _config_with_acme_myapp(_isolate["config_path"])
    pr_db.upsert_notes_sync(
        "acme/myapp", 42, "carry me over", "2026-05-22T00:00:00Z",
        db_path=_isolate["db_path"],
    )

    from app.routes import bookmarks as bookmarks_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {
            "title": "fix the thing",
            "author": {"login": "alice"},
            "url": "https://github.com/acme/myapp/pull/42",
            "state": "OPEN",
        }

    monkeypatch.setattr(bookmarks_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post(
            "/api/bookmarks",
            json={"url": "https://github.com/acme/myapp/pull/42"},
        )
    assert r.status_code == 201, r.text
    assert r.json()["notes"] == "carry me over"


# --- GET /api/bookmarks (list) -----------------------------------------


def test_list_bookmarks_empty(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.get("/api/bookmarks")
    assert r.status_code == 200
    assert r.json() == {"bookmarks": []}


def test_list_bookmarks_returns_persisted_rows(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=1, title="one",
    )
    with TestClient(app) as client:
        r = client.get("/api/bookmarks")
    body = r.json()
    assert len(body["bookmarks"]) == 1
    assert body["bookmarks"][0]["title"] == "one"


def test_list_bookmarks_orders_newest_first(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"], pr_repo="o/r", pr_number=1,
        bookmarked_at="2026-05-01T00:00:00Z",
    )
    seed_bookmark(
        _isolate["db_path"], pr_repo="o/r", pr_number=2,
        bookmarked_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        r = client.get("/api/bookmarks")
    nums = [b["pr_number"] for b in r.json()["bookmarks"]]
    assert nums == [2, 1]


# --- DELETE /api/bookmarks ---------------------------------------------


def test_delete_bookmark_happy_path(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(_isolate["db_path"], pr_repo="acme/myapp", pr_number=1)
    with TestClient(app) as client:
        r = client.delete("/api/bookmarks/acme/myapp/1")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}
    # GC should have evaporated the row (no other flag / notes / worktree).
    assert pr_db.get_pr_sync("acme/myapp", 1, db_path=_isolate["db_path"]) is None


def test_delete_bookmark_404_when_missing(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.delete("/api/bookmarks/acme/myapp/999")
    assert r.status_code == 404


# --- PUT /api/bookmarks/.../notes --------------------------------------


def test_update_bookmark_notes(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(_isolate["db_path"], pr_repo="acme/myapp", pr_number=1)
    with TestClient(app) as client:
        r = client.put(
            "/api/bookmarks/acme/myapp/1/notes",
            json={"notes": "tracking this for follow-up"},
        )
    assert r.status_code == 200
    pr = pr_db.get_pr_sync(
        "acme/myapp", 1, db_path=_isolate["db_path"]
    )
    assert pr is not None
    assert pr.notes == "tracking this for follow-up"


def test_update_bookmark_notes_404_when_missing(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/bookmarks/acme/myapp/1/notes",
            json={"notes": "x"},
        )
    assert r.status_code == 404


# --- POST /api/bookmarks/.../pull-down ----------------------------------


def test_pull_down_bookmark_404_when_not_bookmarked(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post("/api/bookmarks/acme/myapp/42/pull-down")
    assert r.status_code == 404


def test_pull_down_bookmark_happy_path(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    import subprocess

    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
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

    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=42,
        author_login="alice",
    )

    from app.services import pull_down

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/x", "isCrossRepository": False}

    monkeypatch.setattr(pull_down, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post("/api/bookmarks/acme/myapp/42/pull-down")
    assert r.status_code == 200, r.text

    # Drain the background setup task before reading the row (plan-67
    # made pull-down return as soon as the setting_up row is inserted).
    import asyncio
    asyncio.run(wt_svc.wait_for_setup_complete())

    # Bookmark's author_login was written to the unified pr row;
    # the worktree projects it via LEFT JOIN at read time.
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
    assert row == ("alice",)

    # Pulling down consumes the bookmark: the worktree now holds the row,
    # so a later worktree delete GC's it entirely (see the worktree-delete
    # tests). The bookmarked_at audit trail is preserved.
    pr = pr_db.get_pr_sync("acme/myapp", 42, db_path=_isolate["db_path"])
    assert pr is not None
    assert pr.is_bookmarked is False
    assert pr.bookmarked_at is not None
