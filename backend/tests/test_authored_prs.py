"""Tests for the authored-PR slice (plan-48, Slice C)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import authored_prs
from tests.fixtures.bookmark import seed_bookmark
from tests.fixtures.config import write_minimal_config, write_repo_config
from tests.fixtures.inbox import seed_inbox_row
from tests.fixtures.worktree import seed_worktree


def _gh_entry(
    *,
    repo: str = "acme/myapp",
    number: int = 1,
    title: str = "feat: thing",
    updated: str = "2026-05-20T00:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "updatedAt": updated,
        "author": {"login": "me"},
        "repository": {
            "name": repo.split("/", 1)[1],
            "nameWithOwner": repo,
        },
        "state": "OPEN",
    }


# --- fetch_authored_prs --------------------------------------------------


def test_fetch_authored_prs_empty_when_gh_returns_empty(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return []

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert result == []


def test_fetch_authored_prs_returns_open_authored_rows(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [_gh_entry(repo="acme/myapp", number=42, title="my pr")]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert len(result) == 1
    assert result[0].pr_repo == "acme/myapp"
    assert result[0].pr_number == 42
    assert result[0].title == "my pr"
    assert result[0].repo_configured is False


def test_fetch_authored_prs_dedups_against_worktree(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_worktree(
        _isolate["db_path"],
        "myapp", "feat1",
        branch="feat/x",
        pr_repo="acme/myapp",
        pr_number=42,
    )

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [
            _gh_entry(repo="acme/myapp", number=42),
            _gh_entry(repo="acme/myapp", number=43),
        ]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [43]


def test_fetch_authored_prs_dedups_against_inbox(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a PR somehow ended up in the inbox (e.g., the user was both
    author and review-requested), don't double-render it in the
    authored tier."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=42,
    )

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [
            _gh_entry(repo="acme/myapp", number=42),
            _gh_entry(repo="acme/myapp", number=43),
        ]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [43]


def test_fetch_authored_prs_dedups_against_bookmarks(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authored PR the user manually bookmarked should only render
    in the Bookmarks section."""
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=42,
    )

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [
            _gh_entry(repo="acme/myapp", number=42),
            _gh_entry(repo="acme/myapp", number=43),
        ]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [43]


def test_fetch_authored_prs_extracts_ticket(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"], tmp_path, repo_path,
        name="myapp",
        ticket_pattern=r"[A-Z]+-\d+",
    )

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [
            _gh_entry(
                repo="acme/myapp", number=1, title="PROJ-218: do thing",
            ),
        ]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert result[0].ticket == "PROJ-218"


def test_fetch_authored_prs_sets_repo_configured_flag(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"], tmp_path, repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [
            _gh_entry(repo="acme/myapp", number=1),
            _gh_entry(repo="other/elsewhere", number=2),
        ]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    by_num = {r.pr_number: r for r in result}
    assert by_num[1].repo_configured is True
    assert by_num[2].repo_configured is False


def test_fetch_authored_prs_sorts_newest_first(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return [
            _gh_entry(number=1, updated="2026-05-01T00:00:00Z"),
            _gh_entry(number=2, updated="2026-05-20T00:00:00Z"),
            _gh_entry(number=3, updated="2026-05-10T00:00:00Z"),
        ]

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [2, 3, 1]


# --- GET /api/authored-prs ---------------------------------------------


def test_list_endpoint_empty(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    # Stub the gh call so the test doesn't accidentally hit the
    # user's real ``gh search prs --author:@me``.
    async def fake_run_gh_json(args: list, **kwargs: Any) -> list:
        return []

    monkeypatch.setattr(authored_prs, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.get("/api/authored-prs")
    assert r.status_code == 200
    assert r.json() == {"authored_prs": []}


def test_list_endpoint_returns_rows(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    async def fake_fetch() -> list:
        from app.models.authored_pr import AuthoredPrRow
        return [
            AuthoredPrRow(
                pr_repo="acme/myapp",
                pr_number=42,
                title="my pr",
                url="https://github.com/acme/myapp/pull/42",
                is_draft=False,
                ci_status="pass",
                ticket=None,
                pr_updated_at="2026-05-20T00:00:00Z",
                repo_configured=True,
            ),
        ]

    from app.routes import authored_prs as authored_route

    monkeypatch.setattr(authored_route, "fetch_authored_prs_safe", fake_fetch)

    with TestClient(app) as client:
        r = client.get("/api/authored-prs")
    body = r.json()
    assert len(body["authored_prs"]) == 1
    assert body["authored_prs"][0]["pr_number"] == 42


# --- POST /api/authored-prs/.../pull-down -------------------------------


def test_pull_down_authored_400_when_repo_not_configured(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    from app.routes import authored_prs as authored_route

    async def fake_user_login() -> str:
        return "me"

    monkeypatch.setattr(authored_route, "get_user_login", fake_user_login)

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

    from app.routes import authored_prs as authored_route
    from app.routes import inbox as inbox_route

    async def fake_user_login() -> str:
        return "me"

    monkeypatch.setattr(authored_route, "get_user_login", fake_user_login)

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/x", "isCrossRepository": False}

    monkeypatch.setattr(inbox_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post("/api/authored-prs/acme/myapp/42/pull-down")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "myapp"

    # Verify the worktree row got @me as pr_author_login (resolved via
    # get_user_login at the route layer).
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        row = conn.execute(
            "SELECT pr_author_login FROM worktree WHERE repo=?",
            ("myapp",),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("me",)
