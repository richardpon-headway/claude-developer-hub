"""Tests for the authored-PR routes."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import pr_db
from tests.fixtures.bookmark import seed_bookmark
from tests.fixtures.config import write_minimal_config, write_repo_config
from tests.fixtures.inbox import seed_inbox_row
from tests.fixtures.pr import seed_pr
from tests.fixtures.worktree import seed_worktree


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


def _list_authored(client: TestClient) -> list[dict]:
    r = client.get("/api/authored-prs")
    assert r.status_code == 200, r.text
    return r.json()["authored_prs"]


# --- GET /api/authored-prs ---------------------------------------------


def test_list_empty_when_no_rows(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert rows == []


def test_list_returns_open_authored_rows(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        title="my pr",
        author_login="me",
        state="open",
        url="https://github.com/acme/myapp/pull/42",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert len(rows) == 1
    assert rows[0]["pr_repo"] == "acme/myapp"
    assert rows[0]["pr_number"] == 42
    assert rows[0]["title"] == "my pr"
    assert rows[0]["repo_configured"] is False


def test_list_excludes_worktreed(_isolate: dict[str, Path]) -> None:
    """An authored PR pulled down into a worktree drops out of the
    authored surface (it lives in Workspaces instead)."""
    write_minimal_config(_isolate["config_path"])
    seed_worktree(
        _isolate["db_path"],
        "myapp", "feat1",
        branch="feat/x",
        pr_repo="acme/myapp",
        pr_number=42,
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=43,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-21T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert [r["pr_number"] for r in rows] == [43]


def test_list_excludes_inboxed(_isolate: dict[str, Path]) -> None:
    """If a PR somehow ended up in the inbox (the user was both
    author and review-requested), don't double-render it on the
    authored surface."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=42,
        author_login="me",
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=43,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-21T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert [r["pr_number"] for r in rows] == [43]


def test_list_excludes_bookmarked(_isolate: dict[str, Path]) -> None:
    """An authored PR the user manually bookmarked only renders on
    the bookmark surface."""
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=42,
        author_login="me",
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=43,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-21T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert [r["pr_number"] for r in rows] == [43]


def test_list_extracts_ticket_when_pr_ticket_null(
    _isolate: dict[str, Path], tmp_path: Path,
) -> None:
    """When pr.ticket is NULL but the configured repo has a ticket
    pattern, the route layer extracts it from the title on the fly."""
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"], tmp_path, repo_path,
        name="myapp",
        ticket_pattern=r"[A-Z]+-\d+",
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=1,
        title="PROJ-218: do thing",
        author_login="me",
        state="open",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert rows[0]["ticket"] == "PROJ-218"


def test_list_sets_repo_configured_flag(
    _isolate: dict[str, Path], tmp_path: Path,
) -> None:
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"], tmp_path, repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=1,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    seed_pr(
        _isolate["db_path"],
        pr_repo="other/elsewhere",
        pr_number=2,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    by_num = {r["pr_number"]: r for r in rows}
    assert by_num[1]["repo_configured"] is True
    assert by_num[2]["repo_configured"] is False


def test_list_sorts_newest_first(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    for n, updated in [
        (1, "2026-05-01T00:00:00Z"),
        (2, "2026-05-20T00:00:00Z"),
        (3, "2026-05-10T00:00:00Z"),
    ]:
        seed_pr(
            _isolate["db_path"],
            pr_repo="acme/myapp",
            pr_number=n,
            author_login="me",
            state="open",
            pr_updated_at=updated,
        )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert [r["pr_number"] for r in rows] == [2, 3, 1]


def test_list_attaches_notes(_isolate: dict[str, Path]) -> None:
    """A pr row with author_login=me + state=open + notes set + no
    other origin flag surfaces on the authored tier with notes intact."""
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        title="my pr",
        author_login="me",
        state="open",
        notes="remember this",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert len(rows) == 1
    assert rows[0]["notes"] == "remember this"


def test_list_notes_none_when_no_notes(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list_authored(client)
    assert rows[0]["notes"] is None


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

    from app.routes import inbox as inbox_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/x", "isCrossRepository": False}

    monkeypatch.setattr(inbox_route, "run_gh_json", fake_run_gh_json)

    with TestClient(app) as client:
        r = client.post("/api/authored-prs/acme/myapp/42/pull-down")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "myapp"

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
