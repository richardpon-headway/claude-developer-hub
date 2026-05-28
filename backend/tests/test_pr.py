"""Core unit tests for the unified ``pr`` table service module.

Exercises the upsert / flag-toggle / GC / list-filter surface on
``pr_db`` directly, without going through the legacy shim modules.
The shim modules have their own roundtrip tests in
``test_bookmarks.py`` / ``test_inbox.py`` / ``test_authored_prs.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models.pr import PrRow
from app.services import pr_db
from tests.fixtures.worktree import seed_worktree


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app import db

    p = tmp_path / "cdh-test.db"
    monkeypatch.setenv("CDH_DB_PATH", str(p))
    db.apply_migrations_sync(p)
    return p


def test_upsert_inserts_new_row(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_bookmarked=True,
            bookmarked_at="2026-01-01T00:00:00Z",
            title="hi",
            author_login="alice",
            url="https://gh/o/r/pull/1",
            state="open",
        ),
        db_path=db_path,
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.is_bookmarked is True
    assert pr.title == "hi"
    assert pr.state == "open"


def test_upsert_origin_flags_use_max(db_path: Path) -> None:
    """A second upsert that sets is_inbox=True doesn't wipe an earlier
    is_bookmarked=True. Flags accumulate; only the explicit setters
    clear them."""
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_bookmarked=True,
            bookmarked_at="2026-01-01T00:00:00Z",
        ),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_inbox=True,
            inbox_added_at="2026-01-02T00:00:00Z",
            inbox_sources=["reviewer"],
        ),
        db_path=db_path,
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.is_bookmarked is True
    assert pr.is_inbox is True
    assert pr.bookmarked_at == "2026-01-01T00:00:00Z"
    assert pr.inbox_added_at == "2026-01-02T00:00:00Z"
    assert pr.inbox_sources == ["reviewer"]


def test_upsert_scalar_coalesce_keeps_existing_on_null(db_path: Path) -> None:
    """An upsert that doesn't carry a value (e.g., a flag-only flip)
    doesn't blank out fields a prior source set."""
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_inbox=True,
            title="first",
            author_login="alice",
        ),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_archived=True,
            archived_at="2026-02-01T00:00:00Z",
            # No title or author_login on this upsert — must not wipe.
        ),
        db_path=db_path,
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.title == "first"
    assert pr.author_login == "alice"
    assert pr.is_archived is True


def test_set_bookmark_flag_toggle(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_bookmarked=True,
            bookmarked_at="2026-01-01T00:00:00Z",
        ),
        db_path=db_path,
    )
    assert pr_db.set_bookmark_flag_sync(
        "o/r", 1, False, db_path=db_path
    ) == 1
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.is_bookmarked is False
    # bookmarked_at audit trail preserved when clearing.
    assert pr.bookmarked_at == "2026-01-01T00:00:00Z"


def test_set_archived_flag_is_idempotent_on_archived_at(db_path: Path) -> None:
    """Setting archived twice keeps the first ``archived_at`` —
    matches the legacy ``INSERT OR IGNORE INTO inbox_archived``
    contract that route handlers rely on for idempotent dismissals."""
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_inbox=True),
        db_path=db_path,
    )
    pr_db.set_archived_flag_sync(
        "o/r", 1, True, archived_at="2026-01-01T00:00:00Z",
        db_path=db_path,
    )
    pr_db.set_archived_flag_sync(
        "o/r", 1, True, archived_at="2026-02-01T00:00:00Z",
        db_path=db_path,
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.archived_at == "2026-01-01T00:00:00Z"


def test_maybe_gc_deletes_when_no_flag_no_notes_no_worktree(
    db_path: Path,
) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_bookmarked=True),
        db_path=db_path,
    )
    pr_db.set_bookmark_flag_sync("o/r", 1, False, db_path=db_path)
    assert pr_db.maybe_gc_sync("o/r", 1, db_path=db_path) == 1
    assert pr_db.get_pr_sync("o/r", 1, db_path=db_path) is None


def test_maybe_gc_preserves_when_notes_present(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, notes="don't delete me"),
        db_path=db_path,
    )
    assert pr_db.maybe_gc_sync("o/r", 1, db_path=db_path) == 0
    assert pr_db.get_pr_sync("o/r", 1, db_path=db_path) is not None


def test_maybe_gc_preserves_when_worktree_attached(
    db_path: Path, tmp_path: Path
) -> None:
    seed_worktree(
        db_path,
        "myrepo",
        "wt1",
        path=tmp_path / "wt1",
        pr_repo="o/r",
        pr_number=1,
    )
    # seed_worktree above already inserted a stub pr row (FK requires
    # it). maybe_gc must see the worktree and refuse to delete.
    assert pr_db.maybe_gc_sync("o/r", 1, db_path=db_path) == 0


def test_list_pr_filters_by_origin_flag(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_bookmarked=True,
              bookmarked_at="2026-01-01T00:00:00Z"),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=2, is_inbox=True,
              inbox_added_at="2026-01-02T00:00:00Z"),
        db_path=db_path,
    )
    bookmarked = pr_db.list_pr_sync(is_bookmarked=True, db_path=db_path)
    assert [r.pr_number for r in bookmarked] == [1]

    inboxed = pr_db.list_pr_sync(is_inbox=True, db_path=db_path)
    assert [r.pr_number for r in inboxed] == [2]


def test_list_pr_excludes_archived_when_requested(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_inbox=True),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=2, is_inbox=True, is_archived=True,
              archived_at="2026-01-02T00:00:00Z"),
        db_path=db_path,
    )
    active = pr_db.list_pr_sync(
        is_inbox=True, is_archived=False, db_path=db_path
    )
    assert [r.pr_number for r in active] == [1]


def test_list_pr_has_worktree_filter(db_path: Path, tmp_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_inbox=True),
        db_path=db_path,
    )
    seed_worktree(
        db_path,
        "myrepo",
        "wt2",
        path=tmp_path / "wt2",
        pr_repo="o/r",
        pr_number=2,
    )
    attached = pr_db.list_pr_sync(has_worktree=True, db_path=db_path)
    assert [r.pr_number for r in attached] == [2]
    detached = pr_db.list_pr_sync(has_worktree=False, db_path=db_path)
    assert [r.pr_number for r in detached] == [1]


def test_keys_helpers_partition_by_flag(db_path: Path, tmp_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_bookmarked=True),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=2, is_inbox=True),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=3, is_archived=True),
        db_path=db_path,
    )
    seed_worktree(
        db_path,
        "myrepo",
        "wt4",
        path=tmp_path / "wt4",
        pr_repo="o/r",
        pr_number=4,
    )
    assert pr_db.bookmarked_keys_sync(db_path=db_path) == {("o/r", 1)}
    assert pr_db.inbox_keys_sync(db_path=db_path) == {("o/r", 2)}
    assert pr_db.archived_keys_sync(db_path=db_path) == {("o/r", 3)}
    assert pr_db.worktree_attached_keys_sync(db_path=db_path) == {("o/r", 4)}


def test_touch_last_seen_bulk(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_inbox=True,
              last_seen_at="2026-01-01T00:00:00Z"),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=2, is_inbox=True,
              last_seen_at="2026-01-01T00:00:00Z"),
        db_path=db_path,
    )
    pr_db.touch_last_seen_sync(
        [("o/r", 1), ("o/r", 2)],
        "2026-02-01T00:00:00Z",
        db_path=db_path,
    )
    p1 = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    p2 = pr_db.get_pr_sync("o/r", 2, db_path=db_path)
    assert p1 is not None and p1.last_seen_at == "2026-02-01T00:00:00Z"
    assert p2 is not None and p2.last_seen_at == "2026-02-01T00:00:00Z"


def test_list_stale_inbox_filters_and_orders(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_inbox=True,
              last_seen_at="2026-01-01T00:00:00Z"),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=2, is_inbox=True,
              last_seen_at="2026-01-02T00:00:00Z"),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=3, is_inbox=True,
              last_seen_at="2026-02-01T00:00:00Z"),
        db_path=db_path,
    )
    stale = pr_db.list_stale_inbox_sync(
        cutoff="2026-01-15T00:00:00Z", limit=10, db_path=db_path
    )
    assert stale == [("o/r", 1), ("o/r", 2)]


def test_delete_notes_through_authored_shim_only_clears_when_no_flag(
    db_path: Path,
) -> None:
    """The authored-PR shim's delete_notes is a no-op when an origin
    flag holds the row — pinned here to prevent regression where
    deleting authored notes wipes bookmark notes."""
    from app.services import authored_pr_notes_db

    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_bookmarked=True,
            bookmarked_at="2026-01-01T00:00:00Z",
            notes="bookmark-notes",
        ),
        db_path=db_path,
    )
    rowcount = authored_pr_notes_db.delete_notes_sync(
        "o/r", 1, db_path=db_path
    )
    assert rowcount == 0
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.notes == "bookmark-notes"
