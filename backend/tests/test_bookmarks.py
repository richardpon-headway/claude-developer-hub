"""Tests for the bookmark slice (plan-48, Slice B)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import bookmark_db, bookmark_poll, inbox_db, inbox_poll
from app.services.bookmark_db import BookmarkExistsError
from app.services.inbox_search import InboxPrRaw
from tests.fixtures.bookmark import build_bookmark_row, seed_bookmark
from tests.fixtures.config import write_minimal_config, write_repo_config
from tests.fixtures.inbox import build_raw_pr

# --- bookmark_db helpers -------------------------------------------------


def test_list_bookmarks_ordered_newest_first(_isolate: dict[str, Path]) -> None:
    seed_bookmark(
        _isolate["db_path"], pr_repo="o/r", pr_number=1,
        bookmarked_at="2026-05-01T00:00:00Z",
    )
    seed_bookmark(
        _isolate["db_path"], pr_repo="o/r", pr_number=2,
        bookmarked_at="2026-05-20T00:00:00Z",
    )
    rows = bookmark_db.list_bookmarks_sync(db_path=_isolate["db_path"])
    assert [r.pr_number for r in rows] == [2, 1]


def test_insert_bookmark_raises_on_duplicate(
    _isolate: dict[str, Path],
) -> None:
    seed_bookmark(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    with pytest.raises(BookmarkExistsError):
        bookmark_db.insert_bookmark_sync(
            build_bookmark_row(pr_repo="o/r", pr_number=1),
            db_path=_isolate["db_path"],
        )


def test_delete_bookmark_returns_rowcount(
    _isolate: dict[str, Path],
) -> None:
    seed_bookmark(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    assert bookmark_db.delete_bookmark_sync(
        "o/r", 1, db_path=_isolate["db_path"]
    ) == 1
    assert bookmark_db.delete_bookmark_sync(
        "o/r", 1, db_path=_isolate["db_path"]
    ) == 0


def test_refresh_bookmark_state_preserves_notes_and_bookmarked_at(
    _isolate: dict[str, Path],
) -> None:
    seed_bookmark(
        _isolate["db_path"], pr_repo="o/r", pr_number=1,
        notes="my note",
        bookmarked_at="2026-05-01T00:00:00Z",
        state="open",
        title="old",
    )
    bookmark_db.refresh_bookmark_state_sync(
        "o/r", 1,
        state="merged",
        title="new",
        author_login="other",
        ticket=None,
        last_refreshed_at="2026-05-21T00:00:00Z",
        db_path=_isolate["db_path"],
    )
    row = bookmark_db.get_bookmark_sync("o/r", 1, db_path=_isolate["db_path"])
    assert row is not None
    assert row.state == "merged"
    assert row.title == "new"
    assert row.author_login == "other"
    assert row.notes == "my note"                          # preserved
    assert row.bookmarked_at == "2026-05-01T00:00:00Z"     # preserved
    assert row.last_refreshed_at == "2026-05-21T00:00:00Z"


def test_update_bookmark_notes_rowcount(_isolate: dict[str, Path]) -> None:
    assert bookmark_db.update_bookmark_notes_sync(
        "o/r", 1, "x", db_path=_isolate["db_path"]
    ) == 0
    seed_bookmark(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    assert bookmark_db.update_bookmark_notes_sync(
        "o/r", 1, "x", db_path=_isolate["db_path"]
    ) == 1


def test_bookmark_pr_keys_returns_set(_isolate: dict[str, Path]) -> None:
    seed_bookmark(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    seed_bookmark(_isolate["db_path"], pr_repo="o/r", pr_number=2)
    assert bookmark_db.bookmark_pr_keys_sync(
        db_path=_isolate["db_path"]
    ) == {("o/r", 1), ("o/r", 2)}


# --- POST /api/bookmarks (add) -----------------------------------------


def test_add_bookmark_400_on_bad_url(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post("/api/bookmarks", json={"url": "not a url"})
    assert r.status_code == 400
    assert "github.com" in r.json()["detail"].lower()


def test_add_bookmark_happy_path(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

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
    write_minimal_config(_isolate["config_path"])
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
    write_minimal_config(_isolate["config_path"])

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
    write_minimal_config(_isolate["config_path"])

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


# --- DELETE /api/bookmarks ---------------------------------------------


def test_delete_bookmark_happy_path(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(_isolate["db_path"], pr_repo="acme/myapp", pr_number=1)
    with TestClient(app) as client:
        r = client.delete("/api/bookmarks/acme/myapp/1")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}
    assert bookmark_db.list_bookmarks_sync(db_path=_isolate["db_path"]) == []


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
    row = bookmark_db.get_bookmark_sync(
        "acme/myapp", 1, db_path=_isolate["db_path"]
    )
    assert row is not None
    assert row.notes == "tracking this for follow-up"


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


# --- bookmark_poll -----------------------------------------------------


def test_bookmark_poll_refreshes_state(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=1,
        state="open",
        title="old",
        author_login="alice",
        notes="my note",
        bookmarked_at="2026-05-01T00:00:00Z",
        last_refreshed_at="2026-05-01T00:00:00Z",
    )

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {
            "title": "new title",
            "author": {"login": "bob"},
            "state": "MERGED",
        }

    monkeypatch.setattr(bookmark_poll, "run_gh_json", fake_run_gh_json)

    import asyncio

    asyncio.run(bookmark_poll._tick())

    row = bookmark_db.get_bookmark_sync(
        "acme/myapp", 1, db_path=_isolate["db_path"]
    )
    assert row is not None
    assert row.state == "merged"
    assert row.title == "new title"
    assert row.author_login == "bob"
    assert row.notes == "my note"                          # preserved
    assert row.bookmarked_at == "2026-05-01T00:00:00Z"     # preserved
    assert row.last_refreshed_at != "2026-05-01T00:00:00Z"  # advanced


def test_bookmark_poll_noop_when_empty(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    import asyncio

    asyncio.run(bookmark_poll._tick())  # must not raise


# --- inbox poll dedup against bookmarks -------------------------------


def test_inbox_tick_skips_bookmarked_prs(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR that's both review-requested AND bookmarked should NOT
    appear in the inbox — bookmark wins (explicit user pin)."""
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"], pr_repo="o/r", pr_number=42,
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return [
            build_raw_pr(repo="o/r", number=42, head="feat/x"),
            build_raw_pr(repo="o/r", number=43, head="feat/y"),
        ]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    rows = inbox_db.list_inbox_sync(db_path=_isolate["db_path"])
    assert [(r.pr_repo, r.pr_number) for r in rows] == [("o/r", 43)]
