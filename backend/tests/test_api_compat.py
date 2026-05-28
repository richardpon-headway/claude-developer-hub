"""Wire-shape snapshot tests for the four PR-surface list endpoints.

Seeds one pr row per surface (bookmark / inbox / authored / worktreed)
and asserts each list endpoint's JSON response matches a checked-in
expected dict verbatim. The FE type definitions in
``frontend/src/api/types.ts`` are hand-maintained; this test catches
field-rename / field-drop / field-shape drift between BE and FE.

Deletable after the unified UI flip ships and we move to a generated
TypeScript client.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fixtures.bookmark import seed_bookmark
from tests.fixtures.config import write_minimal_config
from tests.fixtures.inbox import seed_inbox_row
from tests.fixtures.pr import seed_pr
from tests.fixtures.worktree import seed_worktree


@pytest.fixture(autouse=True)
def _stub_user_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot the authored + worktrees endpoints with a deterministic
    user_login so the assertion below stays stable."""
    from app.routes import authored_prs as authored_route
    from app.services import gh_identity

    async def fake() -> str:
        return "me"

    gh_identity.reset_cache()
    monkeypatch.setattr(gh_identity, "get_user_login", fake)
    monkeypatch.setattr(authored_route, "get_user_login", fake)
    yield
    gh_identity.reset_cache()


def test_bookmarks_list_response_shape(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=11,
        title="bookmark me",
        author_login="alice",
        url="https://github.com/acme/myapp/pull/11",
        state="open",
        notes="bookmark notes",
        ticket="PROJ-1",
        bookmarked_at="2026-05-21T00:00:00Z",
        last_refreshed_at="2026-05-22T00:00:00Z",
    )
    with TestClient(app) as client:
        r = client.get("/api/bookmarks")
    assert r.status_code == 200
    assert r.json() == {
        "bookmarks": [
            {
                "pr_repo": "acme/myapp",
                "pr_number": 11,
                "title": "bookmark me",
                "author_login": "alice",
                "url": "https://github.com/acme/myapp/pull/11",
                "state": "open",
                "notes": "bookmark notes",
                "ticket": "PROJ-1",
                "bookmarked_at": "2026-05-21T00:00:00Z",
                "last_refreshed_at": "2026-05-22T00:00:00Z",
            },
        ],
    }


def test_inbox_list_response_shape(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=22,
        title="review me",
        author_login="bob",
        url="https://github.com/acme/myapp/pull/22",
        is_draft=False,
        ci_status="pass",
        sources=["reviewer"],
        notes="inbox notes",
        ticket="PROJ-2",
        pr_updated_at="2026-05-14T00:00:00Z",
        added_at="2026-05-14T00:00:00Z",
        last_seen_at="2026-05-14T00:00:00Z",
    )
    with TestClient(app) as client:
        r = client.get("/api/inbox")
    assert r.status_code == 200
    assert r.json() == {
        "prs": [
            {
                "pr_repo": "acme/myapp",
                "pr_number": 22,
                "title": "review me",
                "author_login": "bob",
                "url": "https://github.com/acme/myapp/pull/22",
                "is_draft": False,
                "ci_status": "pass",
                "sources": ["reviewer"],
                "notes": "inbox notes",
                "ticket": "PROJ-2",
                "pr_updated_at": "2026-05-14T00:00:00Z",
                "added_at": "2026-05-14T00:00:00Z",
                "last_seen_at": "2026-05-14T00:00:00Z",
                "repo_configured": False,
            },
        ],
    }


def test_authored_list_response_shape(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=33,
        title="my own pr",
        author_login="me",
        state="open",
        url="https://github.com/acme/myapp/pull/33",
        is_draft=False,
        ci_status="pass",
        ticket="PROJ-3",
        notes="authored notes",
        pr_updated_at="2026-05-20T00:00:00Z",
    )
    with TestClient(app) as client:
        r = client.get("/api/authored-prs")
    assert r.status_code == 200
    assert r.json() == {
        "authored_prs": [
            {
                "pr_repo": "acme/myapp",
                "pr_number": 33,
                "title": "my own pr",
                "url": "https://github.com/acme/myapp/pull/33",
                "is_draft": False,
                "ci_status": "pass",
                "ticket": "PROJ-3",
                "pr_updated_at": "2026-05-20T00:00:00Z",
                "repo_configured": False,
                "notes": "authored notes",
            },
        ],
    }


def test_worktrees_list_response_shape(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Worktrees endpoint wasn't rewritten by plan-61, but the
    ``pr_author_login`` projection (LEFT JOIN against pr) is on the
    same wire contract as the three rewritten surfaces. Pinned here
    to catch drift if a future plan moves more columns onto the JOIN.
    """
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=44,
        author_login="carol",
    )
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feat_x",
        path=wt_path,
        branch="feat/x",
        status="ready",
        ticket="PROJ-4",
        pr_number=44,
        pr_repo="acme/myapp",
        created_at="2026-05-15T00:00:00Z",
    )
    with TestClient(app) as client:
        r = client.get("/api/worktrees")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert body["user_login"] == "me"
    assert len(body["worktrees"]) == 1
    w = body["worktrees"][0]
    # Worktree wire contract: keys + projected pr_author_login. The
    # path varies (tmp_path) and pr_state is None for a row with no
    # poll history.
    assert w == {
        "repo": "myapp",
        "name": "feat_x",
        "path": str(wt_path),
        "branch": "feat/x",
        "ticket": "PROJ-4",
        "pr_number": 44,
        "pr_repo": "acme/myapp",
        "pr_author_login": "carol",
        "notes": None,
        "created_at": "2026-05-15T00:00:00Z",
        "status": "ready",
        "has_claude_session": False,
        "pr_state": None,
    }
