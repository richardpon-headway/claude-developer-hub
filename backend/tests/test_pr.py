"""Core unit tests for the unified ``pr`` table service module.

Exercises the upsert / flag-toggle / GC / list-filter surface on
``pr_db`` directly. Route-level roundtrips live in ``test_bookmarks.py``
/ ``test_authored_prs.py``.
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


def test_upsert_bookmark_flag_uses_max(db_path: Path) -> None:
    """A later metadata-only upsert (is_bookmarked defaults to 0)
    doesn't wipe an earlier is_bookmarked=True. The flag accumulates;
    only the explicit setter clears it."""
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
            title="enriched",
            author_login="alice",
        ),
        db_path=db_path,
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.is_bookmarked is True
    assert pr.bookmarked_at == "2026-01-01T00:00:00Z"
    assert pr.title == "enriched"


def test_upsert_scalar_coalesce_keeps_existing_on_null(db_path: Path) -> None:
    """An upsert that doesn't carry a value doesn't blank out fields a
    prior source set."""
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            is_bookmarked=True,
            title="first",
            author_login="alice",
        ),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(
            pr_repo="o/r",
            pr_number=1,
            state="open",
            # No title or author_login on this upsert — must not wipe.
        ),
        db_path=db_path,
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.title == "first"
    assert pr.author_login == "alice"
    assert pr.state == "open"


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
        PrRow(pr_repo="o/r", pr_number=2, author_login="alice"),
        db_path=db_path,
    )
    bookmarked = pr_db.list_pr_sync(is_bookmarked=True, db_path=db_path)
    assert [r.pr_number for r in bookmarked] == [1]

    unbookmarked = pr_db.list_pr_sync(is_bookmarked=False, db_path=db_path)
    assert [r.pr_number for r in unbookmarked] == [2]


def test_list_pr_has_worktree_filter(db_path: Path, tmp_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, is_bookmarked=True),
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
    seed_worktree(
        db_path,
        "myrepo",
        "wt4",
        path=tmp_path / "wt4",
        pr_repo="o/r",
        pr_number=4,
    )
    assert pr_db.bookmarked_keys_sync(db_path=db_path) == {("o/r", 1)}
    assert pr_db.worktree_attached_keys_sync(db_path=db_path) == {("o/r", 4)}


def test_touch_last_seen_bulk(db_path: Path) -> None:
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=1, author_login="alice",
              last_seen_at="2026-01-01T00:00:00Z"),
        db_path=db_path,
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=2, author_login="alice",
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


def test_upsert_notes_inserts_stub_when_no_row(db_path: Path) -> None:
    """``pr_db.upsert_notes_sync`` is the authored-PR notes path: notes
    must survive even when no discovery poll has written a pr row yet."""
    pr_db.upsert_notes_sync(
        "o/r", 1, "first note", "2026-05-22T00:00:00Z", db_path=db_path
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.notes == "first note"
    # No origin flag set — gc would evaporate the row if not for the
    # notes column, which the plan-59 GC contract explicitly preserves.
    assert pr_db.maybe_gc_sync("o/r", 1, db_path=db_path) == 0


def test_upsert_notes_overwrites_existing_value(db_path: Path) -> None:
    """An empty-string note must replace (not COALESCE-merge) a prior
    value — explicit clears are user intent."""
    pr_db.upsert_notes_sync(
        "o/r", 1, "first", "2026-05-22T00:00:00Z", db_path=db_path
    )
    pr_db.upsert_notes_sync(
        "o/r", 1, "", "2026-05-22T00:01:00Z", db_path=db_path
    )
    pr = pr_db.get_pr_sync("o/r", 1, db_path=db_path)
    assert pr is not None
    assert pr.notes == ""
