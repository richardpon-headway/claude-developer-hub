"""Tests for the unified GET /api/workspaces endpoint."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.fixtures.bookmark import seed_bookmark
from tests.fixtures.pr import seed_pr
from tests.fixtures.worktree import seed_worktree


@pytest.fixture(autouse=True)
def _stub_user_login(monkeypatch: pytest.MonkeyPatch):
    """Stub the gh-identity lookup the endpoint imported so the bucket
    derivation is deterministic and no real gh call fires."""
    from app.routes import workspaces as workspaces_route

    async def fake() -> str:
        return "me"

    monkeypatch.setattr(workspaces_route, "get_user_login", fake)
    yield


def _list(client: TestClient) -> list[dict]:
    r = client.get("/api/workspaces")
    assert r.status_code == 200, r.text
    return r.json()["workspaces"]


def _by_pr(rows: list[dict], pr_number: int) -> dict:
    matches = [w for w in rows if w["pr_number"] == pr_number]
    assert len(matches) == 1, f"expected exactly one row for #{pr_number}, got {len(matches)}"
    return matches[0]


def test_user_login_echoed(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.get("/api/workspaces")
    assert r.status_code == 200
    assert r.json()["user_login"] == "me"


def test_authored_open_no_worktree_appears_in_my_work(
    _isolate: dict[str, Path],
) -> None:
    seed_pr(
        _isolate["db_path"], pr_repo="acme/app", pr_number=1,
        author_login="me", state="open", title="my pr",
    )
    with TestClient(app) as client:
        rows = _list(client)
    w = _by_pr(rows, 1)
    assert w["author_login"] == "me"
    assert w["worktree"] is None
    assert w["is_bookmarked"] is False


def test_bookmarked_teammate_no_worktree(_isolate: dict[str, Path]) -> None:
    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/app", pr_number=2,
        author_login="alice",
    )
    with TestClient(app) as client:
        rows = _list(client)
    w = _by_pr(rows, 2)
    assert w["author_login"] == "alice"
    assert w["is_bookmarked"] is True
    assert w["worktree"] is None


def test_no_pr_worktree_present_with_null_author(
    _isolate: dict[str, Path],
) -> None:
    seed_worktree(
        _isolate["db_path"], "app", "scratch", branch="scratch",
    )
    with TestClient(app) as client:
        rows = _list(client)
    wt_rows = [w for w in rows if w["worktree"] is not None]
    assert len(wt_rows) == 1
    w = wt_rows[0]
    assert w["pr_number"] is None
    assert w["author_login"] is None
    assert w["worktree"]["name"] == "scratch"


def test_mine_bookmarked_with_worktree_appears_once_with_star(
    _isolate: dict[str, Path],
) -> None:
    """The worktree arm wins the collision and carries is_bookmarked."""
    seed_pr(
        _isolate["db_path"], pr_repo="acme/app", pr_number=3,
        author_login="me", state="open", is_bookmarked=True,
        bookmarked_at="2026-01-01T00:00:00Z",
    )
    seed_worktree(
        _isolate["db_path"], "app", "feat", branch="feat",
        pr_repo="acme/app", pr_number=3,
    )
    with TestClient(app) as client:
        rows = _list(client)
    w = _by_pr(rows, 3)  # exactly one
    assert w["worktree"] is not None
    assert w["is_bookmarked"] is True
    assert w["author_login"] == "me"


def test_bookmarked_and_authored_no_worktree_appears_once(
    _isolate: dict[str, Path],
) -> None:
    seed_pr(
        _isolate["db_path"], pr_repo="acme/app", pr_number=4,
        author_login="me", state="open", is_bookmarked=True,
        bookmarked_at="2026-01-01T00:00:00Z",
    )
    with TestClient(app) as client:
        rows = _list(client)
    _by_pr(rows, 4)  # asserts exactly one


def test_plain_pr_row_not_surfaced(_isolate: dict[str, Path]) -> None:
    """A pr row that is neither bookmarked, authored-by-me-open, nor
    worktree-linked (e.g. a teammate's enriched row with no surface)
    must not appear."""
    seed_pr(
        _isolate["db_path"], pr_repo="acme/app", pr_number=5,
        author_login="alice", state="open",
    )
    with TestClient(app) as client:
        rows = _list(client)
    assert all(w["pr_number"] != 5 for w in rows)


def test_state_scalar_surfaced_for_chip_fallback(
    _isolate: dict[str, Path],
) -> None:
    """state/is_draft come through so the card can chip before the
    enrichment poll populates pr_state."""
    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/app", pr_number=6,
        author_login="alice", state="merged",
    )
    with TestClient(app) as client:
        rows = _list(client)
    w = _by_pr(rows, 6)
    assert w["state"] == "merged"
    assert w["pr_state"] is None  # not enriched in this test
