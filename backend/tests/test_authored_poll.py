"""Tests for the authored-PR discovery loop (plan-60)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import authored_poll, gh_identity, pr_db
from app.services.inbox_search import InboxPrRaw
from tests.fixtures.bookmark import seed_bookmark
from tests.fixtures.config import write_minimal_config
from tests.fixtures.pr import seed_pr


@pytest.fixture(autouse=True)
def _stub_user_login(monkeypatch: pytest.MonkeyPatch):
    """Stub the gh-identity helper so tests don't shell to real gh."""
    async def fake() -> str:
        return "me"

    gh_identity.reset_cache()
    monkeypatch.setattr(gh_identity, "get_user_login", fake)
    yield
    gh_identity.reset_cache()


def _make_raw(
    *,
    pr_repo: str = "acme/myapp",
    pr_number: int = 1,
    title: str = "my pr",
    author: str = "me",
    updated: str = "2026-05-20T00:00:00Z",
) -> InboxPrRaw:
    return InboxPrRaw(
        pr_repo=pr_repo,
        pr_number=pr_number,
        title=title,
        author_login=author,
        head_ref="",
        base_ref="",
        is_draft=False,
        url=f"https://github.com/{pr_repo}/pull/{pr_number}",
        updated_at=updated,
        ci_status="none",
        sources=["author"],
    )


def test_tick_upserts_new_authored_row(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search result that isn't already in `pr` lands as a new row
    with author_login set + no origin flag (authored isn't an
    origin flag)."""
    write_minimal_config(_isolate["config_path"])

    async def fake_fetch() -> list[InboxPrRaw]:
        return [_make_raw(pr_number=42, title="my new pr")]

    monkeypatch.setattr(authored_poll, "fetch_authored_prs_raw", fake_fetch)

    asyncio.run(authored_poll._tick())

    pr = pr_db.get_pr_sync("acme/myapp", 42, db_path=_isolate["db_path"])
    assert pr is not None
    assert pr.author_login == "me"
    assert pr.title == "my new pr"
    assert pr.is_bookmarked is False
    assert pr.is_inbox is False
    assert pr.last_seen_at is not None


def test_tick_does_not_clobber_origin_flags(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search result for a PR that's already bookmarked refreshes
    the metadata but keeps is_bookmarked=True (MAX-on-upsert keeps
    the sticky flag)."""
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        author_login="me",
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return [_make_raw(pr_number=42, title="updated title")]

    monkeypatch.setattr(authored_poll, "fetch_authored_prs_raw", fake_fetch)

    asyncio.run(authored_poll._tick())

    pr = pr_db.get_pr_sync("acme/myapp", 42, db_path=_isolate["db_path"])
    assert pr is not None
    assert pr.is_bookmarked is True
    # Title gets refreshed via the COALESCE-on-non-null upsert.
    assert pr.title == "updated title"


def test_tick_gcs_rows_dropped_from_search(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pr row authored by ``me`` whose last_seen_at falls before the
    tick start AND that has no other surface holding it gets GC'd."""
    write_minimal_config(_isolate["config_path"])
    # Pre-existing authored-only row whose last_seen_at is stale.
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=99,
        author_login="me",
        state="open",
        last_seen_at="2026-01-01T00:00:00Z",
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return []  # search no longer returns #99

    monkeypatch.setattr(authored_poll, "fetch_authored_prs_raw", fake_fetch)

    asyncio.run(authored_poll._tick())

    assert pr_db.get_pr_sync(
        "acme/myapp", 99, db_path=_isolate["db_path"]
    ) is None


def test_tick_does_not_gc_rows_held_by_another_surface(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bookmarked PR with a stale last_seen_at must NOT be GC'd
    by the authored sweep — the bookmark flag keeps it alive."""
    write_minimal_config(_isolate["config_path"])
    seed_bookmark(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=99,
        author_login="me",
    )
    # Force the bookmark's pr row to have a stale last_seen_at.
    pr_db.upsert_pr_sync(
        pr_db.PrRow(
            pr_repo="acme/myapp",
            pr_number=99,
            author_login="me",
            last_seen_at="2026-01-01T00:00:00Z",
        ),
        db_path=_isolate["db_path"],
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return []

    monkeypatch.setattr(authored_poll, "fetch_authored_prs_raw", fake_fetch)

    asyncio.run(authored_poll._tick())

    pr = pr_db.get_pr_sync("acme/myapp", 99, db_path=_isolate["db_path"])
    assert pr is not None
    assert pr.is_bookmarked is True


def test_tick_fails_open_on_missing_gh(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``gh search`` raises GhNotFound, the tick returns without
    running GC — a gh outage must not wipe every authored row."""
    write_minimal_config(_isolate["config_path"])
    seed_pr(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=99,
        author_login="me",
        last_seen_at="2026-01-01T00:00:00Z",
    )

    from app.services.gh_cli import GhNotFound

    async def fake_fetch() -> list[InboxPrRaw]:
        raise GhNotFound("gh not on PATH")

    monkeypatch.setattr(authored_poll, "fetch_authored_prs_raw", fake_fetch)

    asyncio.run(authored_poll._tick())

    # Stale row still present — GC must NOT have fired.
    assert pr_db.get_pr_sync(
        "acme/myapp", 99, db_path=_isolate["db_path"]
    ) is not None


def test_tick_skips_when_no_local_login(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If get_user_login returns None (gh unauthed), the tick is a no-op."""
    write_minimal_config(_isolate["config_path"])

    async def no_login() -> str | None:
        return None

    gh_identity.reset_cache()
    monkeypatch.setattr(gh_identity, "get_user_login", no_login)

    fetch_called = False

    async def fake_fetch() -> list[InboxPrRaw]:
        nonlocal fetch_called
        fetch_called = True
        return []

    monkeypatch.setattr(authored_poll, "fetch_authored_prs_raw", fake_fetch)

    asyncio.run(authored_poll._tick())

    assert fetch_called is False
