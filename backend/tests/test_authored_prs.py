"""Tests for the authored-PR slice (plan-48, Slice C; reshaped in plan-60)."""
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
from tests.fixtures.pr import seed_pr
from tests.fixtures.worktree import seed_worktree


@pytest.fixture(autouse=True)
def _stub_user_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new pr_db-backed ``fetch_authored_prs`` reads
    ``gh_identity.get_user_login`` to scope the query. Stub a fixed
    login so tests don't shell to real gh."""
    from app.services import gh_identity

    async def fake() -> str:
        return "me"

    gh_identity.reset_cache()
    monkeypatch.setattr(gh_identity, "get_user_login", fake)
    yield
    gh_identity.reset_cache()


# --- fetch_authored_prs --------------------------------------------------


def test_fetch_authored_prs_empty_when_no_rows(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert result == []


def test_fetch_authored_prs_returns_open_authored_rows(
    _isolate: dict[str, Path],
) -> None:
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert len(result) == 1
    assert result[0].pr_repo == "acme/myapp"
    assert result[0].pr_number == 42
    assert result[0].title == "my pr"
    assert result[0].repo_configured is False


def test_fetch_authored_prs_excludes_worktreed(
    _isolate: dict[str, Path],
) -> None:
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [43]


def test_fetch_authored_prs_excludes_inboxed(
    _isolate: dict[str, Path],
) -> None:
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [43]


def test_fetch_authored_prs_excludes_bookmarked(
    _isolate: dict[str, Path],
) -> None:
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [43]


def test_fetch_authored_prs_extracts_ticket(
    _isolate: dict[str, Path], tmp_path: Path,
) -> None:
    """When pr.ticket is NULL but the configured repo has a ticket
    pattern, the route layer extracts it from the title on the fly
    (back-compat with old rows; future discovery sets pr.ticket
    directly via the authored_poll upsert)."""
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert result[0].ticket == "PROJ-218"


def test_fetch_authored_prs_sets_repo_configured_flag(
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    by_num = {r.pr_number: r for r in result}
    assert by_num[1].repo_configured is True
    assert by_num[2].repo_configured is False


def test_fetch_authored_prs_sorts_newest_first(
    _isolate: dict[str, Path],
) -> None:
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert [r.pr_number for r in result] == [2, 3, 1]


# --- GET /api/authored-prs ---------------------------------------------


def test_list_endpoint_empty(_isolate: dict[str, Path]) -> None:
    """Empty pr table → empty list. The autouse user-login stub
    means the gh shellout never fires; the pr_db read returns []."""
    write_minimal_config(_isolate["config_path"])

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


# --- authored_pr_notes_db -----------------------------------------------


def test_authored_pr_notes_upsert_then_get(
    _isolate: dict[str, Path],
) -> None:
    from app.services import authored_pr_notes_db

    assert authored_pr_notes_db.get_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) is None

    authored_pr_notes_db.upsert_notes_sync(
        "acme/myapp", 42, "watch this", "2026-05-22T00:00:00Z",
        db_path=_isolate["db_path"],
    )
    assert authored_pr_notes_db.get_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) == "watch this"

    # Upsert overwrites in place.
    authored_pr_notes_db.upsert_notes_sync(
        "acme/myapp", 42, "actually nevermind", "2026-05-22T00:01:00Z",
        db_path=_isolate["db_path"],
    )
    assert authored_pr_notes_db.get_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) == "actually nevermind"


def test_authored_pr_notes_delete_rowcount(
    _isolate: dict[str, Path],
) -> None:
    from app.services import authored_pr_notes_db

    assert authored_pr_notes_db.delete_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) == 0
    authored_pr_notes_db.upsert_notes_sync(
        "acme/myapp", 42, "x", "2026-05-22T00:00:00Z",
        db_path=_isolate["db_path"],
    )
    assert authored_pr_notes_db.delete_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) == 1


def test_authored_pr_notes_by_keys_batch_lookup(
    _isolate: dict[str, Path],
) -> None:
    from app.services import authored_pr_notes_db

    authored_pr_notes_db.upsert_notes_sync(
        "acme/myapp", 1, "one", "2026-05-22T00:00:00Z",
        db_path=_isolate["db_path"],
    )
    authored_pr_notes_db.upsert_notes_sync(
        "acme/myapp", 2, "two", "2026-05-22T00:00:00Z",
        db_path=_isolate["db_path"],
    )
    # Empty input → empty result, no SQL fired.
    assert authored_pr_notes_db.notes_by_keys_sync(
        set(), db_path=_isolate["db_path"]
    ) == {}
    # Mixed hits and misses.
    out = authored_pr_notes_db.notes_by_keys_sync(
        {("acme/myapp", 1), ("acme/myapp", 2), ("acme/myapp", 99)},
        db_path=_isolate["db_path"],
    )
    assert out == {("acme/myapp", 1): "one", ("acme/myapp", 2): "two"}


# --- PUT /api/authored-prs/.../notes ------------------------------------


def test_update_notes_endpoint_upserts(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/authored-prs/acme/myapp/42/notes",
            json={"notes": "blocked on PROJ-1"},
        )
    assert r.status_code == 200
    assert r.json()["notes"] == "blocked on PROJ-1"

    from app.services import authored_pr_notes_db
    assert authored_pr_notes_db.get_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) == "blocked on PROJ-1"


def test_update_notes_accepts_empty_string(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/authored-prs/acme/myapp/42/notes",
            json={"notes": ""},
        )
    assert r.status_code == 200
    from app.services import authored_pr_notes_db
    assert authored_pr_notes_db.get_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) == ""


def test_update_notes_no_404_even_when_no_prior_row(
    _isolate: dict[str, Path],
) -> None:
    """authored rows aren't persisted, so any (pr_repo, pr_number)
    is a valid notes target — first call upserts, no 404 path."""
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/authored-prs/never/heard-of-it/12345/notes",
            json={"notes": "first time"},
        )
    assert r.status_code == 200


# --- fetched authored PRs include notes ---------------------------------


def test_fetch_authored_prs_attaches_notes(
    _isolate: dict[str, Path],
) -> None:
    """A pr row with author_login=me + state=open + notes set + no
    other origin flag surfaces on the authored tier with notes
    intact."""
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

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert len(result) == 1
    assert result[0].notes == "remember this"


def test_fetch_authored_prs_notes_none_when_no_row(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        author_login="me",
        state="open",
        pr_updated_at="2026-05-20T00:00:00Z",
    )

    import asyncio

    result = asyncio.run(authored_prs.fetch_authored_prs())
    assert result[0].notes is None


# --- notes migration on pull-down ---------------------------------------


def test_pull_down_migrates_notes_from_authored_table(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an authored PR has a note, pulling it down should copy the
    note into worktree.notes and drop the authored_pr_notes row."""
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

    # Seed the authored note.
    from app.services import authored_pr_notes_db
    authored_pr_notes_db.upsert_notes_sync(
        "acme/myapp", 42, "I started this, finish it",
        "2026-05-22T00:00:00Z",
        db_path=_isolate["db_path"],
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

    # Note migrated to worktree.
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        notes_col = conn.execute(
            "SELECT notes FROM worktree WHERE repo=?", ("myapp",)
        ).fetchone()
    finally:
        conn.close()
    assert notes_col == ("I started this, finish it",)

    # Source row removed.
    assert authored_pr_notes_db.get_notes_sync(
        "acme/myapp", 42, db_path=_isolate["db_path"]
    ) is None


def test_pull_down_no_authored_note_is_a_noop(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull-down still works when there's no authored note to migrate."""
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
