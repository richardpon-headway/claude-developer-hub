"""Tests for the persistent-inbox slice (plan-48).

The previous ephemeral ``InboxCache`` is gone. Inbox rows live in
SQLite. Tests seed via :func:`tests.fixtures.inbox.seed_inbox_row` and
assert via DB reads or HTTP responses.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config.schema import CDHConfig, InboxConfig
from app.main import app
from app.services import inbox_db, inbox_poll, inbox_search
from app.services.inbox_search import (
    InboxPrRaw,
    _ci_status_from_rollup,
    _row_from_gh,
    configured_repos_index,
    is_repo_configured,
)
from tests.fixtures.config import write_minimal_config, write_repo_config
from tests.fixtures.inbox import (
    build_inbox_row,
    build_raw_pr,
    seed_inbox_row,
)
from tests.fixtures.pr_state import seed_pr_state
from tests.fixtures.worktree import seed_worktree

# --- config schema -------------------------------------------------------


def test_inbox_config_default_empty_teams() -> None:
    """``inbox.teams`` is still validated for back-compat with existing
    YAML configs even though the poller no longer reads it."""
    cfg = CDHConfig()
    assert cfg.inbox.teams == []


def test_inbox_config_team_validation_rejects_bad_slugs() -> None:
    with pytest.raises(Exception) as exc_info:
        InboxConfig(teams=["just-team-name"])
    assert "owner/team" in str(exc_info.value)


def test_inbox_config_accepts_owner_team_format() -> None:
    cfg = InboxConfig(teams=["corp/corrections", "acme/build-team_1"])
    assert cfg.teams == ["corp/corrections", "acme/build-team_1"]


# --- ci status reduction -------------------------------------------------


def test_ci_status_none_when_empty_rollup() -> None:
    assert _ci_status_from_rollup(None) == "none"
    assert _ci_status_from_rollup([]) == "none"


def test_ci_status_fail_beats_pending_beats_pass() -> None:
    rollup = [
        {"state": "SUCCESS"},
        {"conclusion": "FAILURE"},
        {"status": "PENDING"},
    ]
    assert _ci_status_from_rollup(rollup) == "fail"


def test_ci_status_pending_when_no_fail() -> None:
    assert _ci_status_from_rollup([{"state": "SUCCESS"}, {"status": "QUEUED"}]) == "pending"


def test_ci_status_pass_when_all_success() -> None:
    assert _ci_status_from_rollup([{"state": "SUCCESS"}, {"state": "SUCCESS"}]) == "pass"


# --- row parsing ---------------------------------------------------------


def test_row_from_gh_parses_typical_entry() -> None:
    entry = {
        "number": 42,
        "title": "feat: do thing",
        "url": "https://github.com/o/r/pull/42",
        "isDraft": False,
        "updatedAt": "2026-05-14T00:00:00Z",
        "author": {"login": "me"},
        "repository": {"name": "r", "nameWithOwner": "o/r"},
        "state": "OPEN",
    }
    row = _row_from_gh(entry, source="reviewer")
    assert row is not None
    assert row.pr_repo == "o/r"
    assert row.pr_number == 42
    # Placeholders since search doesn't return these fields.
    assert row.head_ref == ""
    assert row.ci_status == "none"
    assert row.sources == ["reviewer"]


def test_row_from_gh_returns_none_on_missing_fields() -> None:
    assert _row_from_gh({}, source="reviewer") is None
    assert _row_from_gh(
        {
            "number": 1,
            "title": "t",
            "url": "https://x",
            "repository": {"name": "r"},
        },
        source="reviewer",
    ) is None


# --- repo configuration matching ---------------------------------------


def test_is_repo_configured_matches_on_basename_fallback() -> None:
    from app.config.schema import RepoConfig

    repos = [RepoConfig(name="myapp", path=Path("/tmp/myapp"))]
    idx = configured_repos_index(repos)
    assert is_repo_configured("acme/myapp", idx) is True
    assert is_repo_configured("acme/other", idx) is False
    assert is_repo_configured("just-a-string", idx) is False


def test_explicit_github_repo_excludes_basename_collisions() -> None:
    from app.config.schema import RepoConfig
    from app.services.inbox_search import lookup_configured_repo

    repos = [
        RepoConfig(
            name="myapp",
            path=Path("/tmp/myapp"),
            github_repo="corp/myapp",
        )
    ]
    idx = configured_repos_index(repos)
    assert lookup_configured_repo("corp/myapp", idx) is not None
    assert lookup_configured_repo("acme/myapp", idx) is None
    assert lookup_configured_repo("corp/other", idx) is None


# --- dedup pull from worktree + pr_state --------------------------------


def test_tracked_keys_reads_from_worktree_columns(_isolate: dict[str, Path]) -> None:
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feat1",
        branch="feat/x",
        pr_repo="o/myapp",
        pr_number=7,
    )
    keys = inbox_poll._tracked_pr_keys_sync()
    assert keys == {("o/myapp", 7)}


def test_tracked_keys_reads_from_pr_state_url(_isolate: dict[str, Path]) -> None:
    """Worktree exists but has no pr_repo — dedup must extract
    owner/name from the pr_state payload URL."""
    seed_worktree(
        _isolate["db_path"], "myapp", "feat1", branch="feat/x"
    )
    seed_pr_state(
        _isolate["db_path"], "myapp", "feat1", pr_number=99, pr_repo="o/myapp"
    )
    keys = inbox_poll._tracked_pr_keys_sync()
    assert keys == {("o/myapp", 99)}


def test_tracked_keys_handles_pr_state_with_malformed_url(
    _isolate: dict[str, Path],
) -> None:
    import json

    seed_worktree(
        _isolate["db_path"], "myapp", "feat1", branch="feat/x"
    )
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        conn.execute(
            "INSERT INTO pr_state (repo, worktree_name, headline, payload, checked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "myapp",
                "feat1",
                "no_pr",
                json.dumps({"pr_number": None, "url": None}),
                "2026-05-14T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert inbox_poll._tracked_pr_keys_sync() == set()


# --- inbox_db helpers ----------------------------------------------------


def test_upsert_inbox_inserts_and_then_updates(_isolate: dict[str, Path]) -> None:
    """Insert then upsert refreshes search-driven fields but preserves
    notes and added_at."""
    row = build_inbox_row(
        pr_repo="o/r",
        pr_number=1,
        title="first",
        notes="my notes",
        added_at="2026-05-14T00:00:00Z",
        last_seen_at="2026-05-14T00:00:00Z",
    )
    inbox_db.upsert_inbox_sync(row, db_path=_isolate["db_path"])

    refreshed = build_inbox_row(
        pr_repo="o/r",
        pr_number=1,
        title="second",
        notes=None,                            # user-edited; must NOT clobber
        added_at="2099-01-01T00:00:00Z",        # must NOT clobber
        last_seen_at="2026-05-21T00:00:00Z",    # MUST advance
        pr_updated_at="2026-05-21T00:00:00Z",   # MUST advance
    )
    inbox_db.upsert_inbox_sync(refreshed, db_path=_isolate["db_path"])

    out = inbox_db.get_inbox_sync("o/r", 1, db_path=_isolate["db_path"])
    assert out is not None
    assert out.title == "second"
    assert out.notes == "my notes"                    # preserved
    assert out.added_at == "2026-05-14T00:00:00Z"     # preserved
    assert out.last_seen_at == "2026-05-21T00:00:00Z"
    assert out.pr_updated_at == "2026-05-21T00:00:00Z"


def test_list_inbox_filters_archived(_isolate: dict[str, Path]) -> None:
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=2)
    inbox_db.archive_inbox_sync(
        "o/r", 2, "2026-05-14T00:00:00Z", db_path=_isolate["db_path"]
    )

    rows = inbox_db.list_inbox_sync(db_path=_isolate["db_path"])
    assert [(r.pr_repo, r.pr_number) for r in rows] == [("o/r", 1)]


def test_archive_is_idempotent(_isolate: dict[str, Path]) -> None:
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    inbox_db.archive_inbox_sync(
        "o/r", 1, "2026-05-14T00:00:00Z", db_path=_isolate["db_path"]
    )
    # Second archive — same PR, must not raise.
    inbox_db.archive_inbox_sync(
        "o/r", 1, "2026-05-21T00:00:00Z", db_path=_isolate["db_path"]
    )
    assert inbox_db.archived_keys_sync(db_path=_isolate["db_path"]) == {("o/r", 1)}


def test_delete_inbox_clears_archive_shadow(_isolate: dict[str, Path]) -> None:
    """Auto-removal sweep deletes both the inbox row and any matching
    inbox_archived row so a future PR with the same number isn't
    silently filtered out."""
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    inbox_db.archive_inbox_sync(
        "o/r", 1, "2026-05-14T00:00:00Z", db_path=_isolate["db_path"]
    )
    inbox_db.delete_inbox_sync("o/r", 1, db_path=_isolate["db_path"])
    assert inbox_db.get_inbox_sync("o/r", 1, db_path=_isolate["db_path"]) is None
    assert inbox_db.archived_keys_sync(db_path=_isolate["db_path"]) == set()


def test_list_stale_inbox_orders_oldest_first(_isolate: dict[str, Path]) -> None:
    seed_inbox_row(
        _isolate["db_path"], pr_repo="o/r", pr_number=1,
        last_seen_at="2026-05-01T00:00:00Z",
    )
    seed_inbox_row(
        _isolate["db_path"], pr_repo="o/r", pr_number=2,
        last_seen_at="2026-05-10T00:00:00Z",
    )
    seed_inbox_row(
        _isolate["db_path"], pr_repo="o/r", pr_number=3,
        last_seen_at="2026-05-20T00:00:00Z",   # not stale vs cutoff
    )
    stale = inbox_db.list_stale_inbox_sync(
        "2026-05-15T00:00:00Z", limit=10, db_path=_isolate["db_path"]
    )
    assert stale == [("o/r", 1), ("o/r", 2)]


# --- source accumulation across queries (auth dropped, team dropped) ----


def test_source_accumulation_across_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR returned by multiple queries accumulates all sources,
    priority-ordered by call order (reviewer > assignee > mentions)."""

    call_count = {"n": 0}

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        call_count["n"] += 1
        if "review-requested:@me" in query:
            return [build_raw_pr(repo="o/r", number=42, head="feat/x", source="reviewer")]
        if "mentions:@me" in query:
            return [build_raw_pr(repo="o/r", number=42, head="feat/x", source="mentions")]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    # Also stub the reviewer post-filter so it doesn't try to call
    # `gh pr view` for the synthetic row.
    async def fake_me_login() -> str | None:
        return None

    monkeypatch.setattr(inbox_search, "_get_me_login", fake_me_login)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    assert len(result) == 1
    assert result[0].sources == ["reviewer", "mentions"]
    # reviewer + assignee + mentions = 3 queries
    assert call_count["n"] == 3


def test_assignee_and_mentions_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        if "assignee:@me" in query:
            return [build_raw_pr(repo="o/r", number=100, head="feat/a", source=source)]
        if "mentions:@me" in query:
            return [build_raw_pr(repo="o/r", number=101, head="feat/m", source=source)]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    async def fake_me_login() -> str | None:
        return None

    monkeypatch.setattr(inbox_search, "_get_me_login", fake_me_login)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    by_number = {p.pr_number: p for p in result}
    assert by_number[100].sources == ["assignee"]
    assert by_number[101].sources == ["mentions"]


# --- reviewer post-filter (drop team-mediated review-requests) ----------


def _patch_me_login(monkeypatch: pytest.MonkeyPatch, login: str | None) -> None:
    async def fake_get() -> str | None:
        return login

    monkeypatch.setattr(inbox_search, "_get_me_login", fake_get)


def _patch_review_requests(
    monkeypatch: pytest.MonkeyPatch,
    review_requests_by_pr: dict[tuple[str, int], list[dict] | None],
) -> None:
    async def fake_is_direct(
        pr_repo: str, pr_number: int, me_login: str
    ) -> bool | None:
        entry = review_requests_by_pr.get((pr_repo, pr_number))
        if entry is None:
            return None  # gh failure
        for r in entry:
            if r.get("__typename") == "User" and r.get("login") == me_login:
                return True
        return False

    monkeypatch.setattr(
        inbox_search, "_is_directly_review_requested", fake_is_direct
    )


def test_reviewer_filter_keeps_direct_user_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_me_login(monkeypatch, "me")
    _patch_review_requests(
        monkeypatch,
        {
            ("o/r", 1): [
                {"__typename": "User", "login": "me"},
                {"__typename": "Team", "slug": "acme/insurance-platform"},
            ],
        },
    )

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        if "review-requested:@me" in query:
            return [build_raw_pr(repo="o/r", number=1, head="feat/a", source="reviewer")]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    assert len(result) == 1
    assert result[0].sources == ["reviewer"]


def test_reviewer_filter_drops_team_mediated_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_me_login(monkeypatch, "me")
    _patch_review_requests(
        monkeypatch,
        {
            ("o/r", 1): [
                {"__typename": "Team", "slug": "acme/insurance-platform"},
                {"__typename": "Team", "slug": "acme/payer"},
            ],
        },
    )

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        if "review-requested:@me" in query:
            return [build_raw_pr(repo="o/r", number=1, head="feat/a", source="reviewer")]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    assert result == []


def test_reviewer_filter_fail_open_on_gh_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_me_login(monkeypatch, "me")
    _patch_review_requests(monkeypatch, {("o/r", 1): None})

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        if "review-requested:@me" in query:
            return [build_raw_pr(repo="o/r", number=1, head="feat/a", source="reviewer")]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    assert len(result) == 1
    assert result[0].sources == ["reviewer"]


def test_reviewer_filter_skipped_when_me_login_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_me_login(monkeypatch, None)

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        if "review-requested:@me" in query:
            return [build_raw_pr(repo="o/r", number=1, head="feat/a", source="reviewer")]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    assert len(result) == 1
    assert result[0].sources == ["reviewer"]


# --- poll tick end-to-end ----------------------------------------------


def test_tick_persists_new_row(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    async def fake_fetch() -> list[InboxPrRaw]:
        return [build_raw_pr(repo="o/r", number=42, head="feat/x")]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    rows = inbox_db.list_inbox_sync(db_path=_isolate["db_path"])
    assert [(r.pr_repo, r.pr_number) for r in rows] == [("o/r", 42)]


def test_tick_dedups_against_worktree(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feat1",
        branch="feat/x",
        pr_repo="o/r",
        pr_number=42,
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


def test_tick_skips_archived_rows(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archived PR re-appearing in gh search results must NOT re-enter
    the active inbox view."""
    write_minimal_config(_isolate["config_path"])
    inbox_db.archive_inbox_sync(
        "o/r", 42, "2026-05-14T00:00:00Z", db_path=_isolate["db_path"]
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return [build_raw_pr(repo="o/r", number=42, head="feat/x")]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    # No row inserted; list filtered (archive shadow exists but inbox row doesn't).
    assert inbox_db.list_inbox_sync(db_path=_isolate["db_path"]) == []


def test_tick_auto_removes_closed_pr(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR that's no longer in gh search results is probed via
    gh pr view; if state != open, the inbox row is deleted."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="o/r",
        pr_number=99,
        last_seen_at="2026-05-01T00:00:00Z",
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return []  # PR no longer appearing in search

    async def fake_pr_state(pr_repo: str, pr_number: int) -> str | None:
        assert (pr_repo, pr_number) == ("o/r", 99)
        return "merged"

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)
    monkeypatch.setattr(inbox_poll, "_gh_pr_state", fake_pr_state)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))
    assert inbox_db.list_inbox_sync(db_path=_isolate["db_path"]) == []


def test_tick_keeps_still_open_stale_row(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-open PR that's no longer in gh search results stays in
    the inbox (sticky); last_seen_at is bumped so we don't re-probe
    every tick."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="o/r",
        pr_number=99,
        last_seen_at="2026-05-01T00:00:00Z",
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return []

    async def fake_pr_state(pr_repo: str, pr_number: int) -> str | None:
        return "open"

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)
    monkeypatch.setattr(inbox_poll, "_gh_pr_state", fake_pr_state)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    rows = inbox_db.list_inbox_sync(db_path=_isolate["db_path"])
    assert len(rows) == 1
    assert rows[0].last_seen_at != "2026-05-01T00:00:00Z"  # bumped


def test_tick_extracts_ticket_via_configured_pattern(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a configured repo defines a ticket_pattern, the poll uses
    it against PR titles for inbox rows too."""
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"],
        tmp_path,
        repo_path,
        name="myapp",
        ticket_pattern=r"[A-Z]+-\d+",
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return [build_raw_pr(
            repo="acme/myapp", number=1, head="feat/x",
            title="PROJ-218: thing",
        )]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))
    rows = inbox_db.list_inbox_sync(db_path=_isolate["db_path"])
    assert len(rows) == 1
    assert rows[0].ticket == "PROJ-218"


# --- endpoint: GET /api/inbox ------------------------------------------


def test_get_inbox_returns_empty_when_db_empty(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.get("/api/inbox")
    assert r.status_code == 200
    assert r.json() == {"prs": []}


def test_get_inbox_returns_persisted_rows(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="o/r",
        pr_number=1,
        title="hello",
        author_login="alice",
        notes="my note",
        ticket="PROJ-1",
    )
    with TestClient(app) as client:
        r = client.get("/api/inbox")
    body = r.json()
    assert len(body["prs"]) == 1
    pr = body["prs"][0]
    assert pr["pr_repo"] == "o/r"
    assert pr["title"] == "hello"
    assert pr["author_login"] == "alice"
    assert pr["notes"] == "my note"
    assert pr["ticket"] == "PROJ-1"
    assert pr["repo_configured"] is False


def test_get_inbox_filters_archived_rows(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=2)
    inbox_db.archive_inbox_sync(
        "o/r", 2, "2026-05-14T00:00:00Z", db_path=_isolate["db_path"]
    )

    with TestClient(app) as client:
        r = client.get("/api/inbox")
    body = r.json()
    nums = sorted(p["pr_number"] for p in body["prs"])
    assert nums == [1]


def test_get_inbox_repo_configured_flag(
    _isolate: dict[str, Path], tmp_path: Path,
) -> None:
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"],
        tmp_path,
        repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )
    seed_inbox_row(_isolate["db_path"], pr_repo="acme/myapp", pr_number=1)
    seed_inbox_row(_isolate["db_path"], pr_repo="other/elsewhere", pr_number=2)

    with TestClient(app) as client:
        r = client.get("/api/inbox")
    by_num = {p["pr_number"]: p for p in r.json()["prs"]}
    assert by_num[1]["repo_configured"] is True
    assert by_num[2]["repo_configured"] is False


# --- endpoint: POST /api/inbox/refresh ----------------------------------


def test_refresh_endpoint_runs_tick_and_returns_rows(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_config(_isolate["config_path"])

    async def fake_fetch() -> list[InboxPrRaw]:
        return [build_raw_pr(repo="o/r", number=99, head="feat/refresh")]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    with TestClient(app) as client:
        r = client.post("/api/inbox/refresh")
    assert r.status_code == 200
    body = r.json()
    assert len(body["prs"]) == 1
    assert body["prs"][0]["pr_number"] == 99


# --- endpoint: archive --------------------------------------------------


def test_archive_endpoint_hides_row_from_list(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    with TestClient(app) as client:
        r1 = client.post("/api/inbox/o/r/1/archive")
        assert r1.status_code == 200

        r2 = client.get("/api/inbox")
        assert r2.json()["prs"] == []


def test_archive_endpoint_is_idempotent(_isolate: dict[str, Path]) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    with TestClient(app) as client:
        client.post("/api/inbox/o/r/1/archive")
        r = client.post("/api/inbox/o/r/1/archive")
    assert r.status_code == 200


def test_archive_endpoint_404_when_row_missing(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post("/api/inbox/o/r/999/archive")
    assert r.status_code == 404


# --- endpoint: notes ----------------------------------------------------


def test_notes_endpoint_updates_row(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    with TestClient(app) as client:
        r = client.put(
            "/api/inbox/o/r/1/notes",
            json={"notes": "blocked on COR-218"},
        )
    assert r.status_code == 200
    assert r.json()["notes"] == "blocked on COR-218"

    row = inbox_db.get_inbox_sync("o/r", 1, db_path=_isolate["db_path"])
    assert row is not None
    assert row.notes == "blocked on COR-218"


def test_notes_endpoint_accepts_empty_string(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"], pr_repo="o/r", pr_number=1, notes="prior"
    )
    with TestClient(app) as client:
        r = client.put(
            "/api/inbox/o/r/1/notes", json={"notes": ""}
        )
    assert r.status_code == 200
    row = inbox_db.get_inbox_sync("o/r", 1, db_path=_isolate["db_path"])
    assert row is not None
    assert row.notes == ""


def test_notes_endpoint_404_when_row_missing(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.put(
            "/api/inbox/o/r/999/notes", json={"notes": "x"}
        )
    assert r.status_code == 404


# --- endpoint: POST /api/inbox/.../pull-down ----------------------------


def test_pull_down_404_when_pr_not_in_inbox(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        r = client.post("/api/inbox/o/r/42/pull-down")
    assert r.status_code == 404


def test_pull_down_400_when_repo_not_configured(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(_isolate["db_path"], pr_repo="acme/other", pr_number=42)
    with TestClient(app) as client:
        r = client.post("/api/inbox/acme/other/42/pull-down")
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_pull_down_400_when_repo_path_missing_on_disk(
    _isolate: dict[str, Path],
) -> None:
    bogus = _isolate["db_path"].parent / "nope"
    write_repo_config(
        _isolate["config_path"],
        None,
        bogus,
        name="myapp",
        github_repo="acme/myapp",
    )
    seed_inbox_row(_isolate["db_path"], pr_repo="acme/myapp", pr_number=42)
    with TestClient(app) as client:
        r = client.post("/api/inbox/acme/myapp/42/pull-down")
    assert r.status_code == 400
    assert "missing on disk" in r.json()["detail"]


def test_pull_down_same_repo_happy_path(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-repo PR: no pre-fetch needed; create_worktree's built-in
    fetch handles it. Verify the worktree row is created and that
    pr_number / pr_repo / pr_author_login were persisted from the
    inbox row's author_login."""
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
        _isolate["config_path"],
        tmp_path,
        repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )

    from app.routes import inbox as inbox_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/x", "isCrossRepository": False}

    monkeypatch.setattr(inbox_route, "run_gh_json", fake_run_gh_json)

    fetch_called = {"n": 0}

    async def fake_fetch_pr_ref(*a: Any, **kw: Any) -> None:
        fetch_called["n"] += 1

    monkeypatch.setattr(inbox_route, "_fetch_pr_ref", fake_fetch_pr_ref)

    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="acme/myapp",
        pr_number=42,
        author_login="sarah-h",
    )

    with TestClient(app) as client:
        r = client.post("/api/inbox/acme/myapp/42/pull-down")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "myapp"
    assert fetch_called["n"] == 0

    conn = sqlite3.connect(_isolate["db_path"])
    try:
        row = conn.execute(
            "SELECT pr_number, pr_repo, pr_author_login FROM worktree WHERE repo=?",
            ("myapp",),
        ).fetchone()
    finally:
        conn.close()
    assert row == (42, "acme/myapp", "sarah-h")


def test_pull_down_fork_pr_fetches_pull_ref(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork PR: must pre-fetch refs/pull/<n>/head into a local branch
    before create_worktree runs."""
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
    write_repo_config(
        _isolate["config_path"],
        tmp_path,
        repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )

    from app.routes import inbox as inbox_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/forked", "isCrossRepository": True}

    fetch_args_seen: list[tuple[Any, ...]] = []

    async def fake_fetch_pr_ref(repo_p: Path, pr_n: int, local_b: str) -> None:
        fetch_args_seen.append((repo_p, pr_n, local_b))
        # Simulate the fork-ref fetch creating the local branch so
        # create_worktree's verify-local step passes.
        import asyncio as _asyncio

        proc = await _asyncio.create_subprocess_exec(
            "git", "-C", str(repo_p), "branch", local_b,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    monkeypatch.setattr(inbox_route, "run_gh_json", fake_run_gh_json)
    monkeypatch.setattr(inbox_route, "_fetch_pr_ref", fake_fetch_pr_ref)

    seed_inbox_row(
        _isolate["db_path"], pr_repo="acme/myapp", pr_number=58,
    )

    with TestClient(app) as client:
        r = client.post("/api/inbox/acme/myapp/58/pull-down")

    assert r.status_code == 200, r.text
    assert len(fetch_args_seen) == 1
    _, pr_n, local_b = fetch_args_seen[0]
    assert pr_n == 58
    assert local_b == "cdh-pr-58-feat/forked"


# --- endpoint: configure-and-pull-down ----------------------------------


def test_configure_and_pull_down_404_when_pr_not_in_inbox(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=object())
        r = client.post("/api/inbox/acme/myapp/42/configure-and-pull-down")
    assert r.status_code == 404


def test_configure_and_pull_down_409_when_repo_already_configured(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    repo_path = tmp_path / "myapp"
    repo_path.mkdir()
    write_repo_config(
        _isolate["config_path"],
        tmp_path,
        repo_path,
        name="myapp",
        github_repo="acme/myapp",
    )
    seed_inbox_row(_isolate["db_path"], pr_repo="acme/myapp", pr_number=42)
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=object())
        r = client.post("/api/inbox/acme/myapp/42/configure-and-pull-down")
    assert r.status_code == 409
    assert "already configured" in r.json()["detail"]


def test_configure_and_pull_down_503_when_iterm_disconnected(
    _isolate: dict[str, Path],
) -> None:
    write_minimal_config(_isolate["config_path"], _isolate["dev_root"])
    seed_inbox_row(_isolate["db_path"], pr_repo="acme/myapp", pr_number=42)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post("/api/inbox/acme/myapp/42/configure-and-pull-down")
    assert r.status_code == 503


def test_configure_and_pull_down_spawns_iterm_returns_session_id(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    dev_root = _isolate["dev_root"]
    write_minimal_config(_isolate["config_path"], dev_root)

    from app.routes import inbox as inbox_route
    from app.routes import repos as repos_route

    spawn_args_seen: dict[str, Any] = {}

    async def fake_spawn(connection, cwd, frame, prompt):  # type: ignore[no-untyped-def]
        spawn_args_seen["cwd"] = cwd
        spawn_args_seen["prompt"] = prompt
        return SimpleNamespace(window_id="W1", claude_session_id="S1")

    monkeypatch.setattr(inbox_route, "spawn_global_claude_window", fake_spawn)

    seed_inbox_row(_isolate["db_path"], pr_repo="acme/myapp", pr_number=42)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=object())
        r = client.post("/api/inbox/acme/myapp/42/configure-and-pull-down")

    assert r.status_code == 200, r.text
    session_id = r.json()["session_id"]
    assert session_id

    assert "Ensure a local clone".lower() in spawn_args_seen["prompt"].lower()
    assert "acme/myapp" in spawn_args_seen["prompt"]
    assert str(dev_root / "myapp") in spawn_args_seen["prompt"]
    assert spawn_args_seen["cwd"] == dev_root

    session = repos_route._sessions[session_id]
    assert session.follow_up == {
        "kind": "pull_down",
        "pr_repo": "acme/myapp",
        "pr_number": 42,
    }


def test_onboard_complete_fires_follow_up_pull_down(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When onboard_complete saves a config entry whose session carries
    a pull_down follow_up, the inbox's _perform_pull_down should be
    invoked in the background with the stored pr_repo + pr_number."""
    import subprocess

    dev_root = _isolate["dev_root"]
    repo_path = dev_root / "myapp"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    write_minimal_config(_isolate["config_path"], dev_root)

    from app.routes import inbox as inbox_route
    from app.routes import repos as repos_route

    async def fake_spawn(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(window_id="W1", claude_session_id="S1")

    monkeypatch.setattr(inbox_route, "spawn_global_claude_window", fake_spawn)

    pull_down_call: dict[str, Any] = {}

    async def fake_pull_down(
        pr_repo: str, pr_number: int, *, author_login: str | None = None
    ) -> Any:
        pull_down_call["args"] = (pr_repo, pr_number)
        pull_down_call["author_login"] = author_login
        return SimpleNamespace(repo="myapp", name="feat_x")

    monkeypatch.setattr(inbox_route, "_perform_pull_down", fake_pull_down)

    seed_inbox_row(_isolate["db_path"], pr_repo="acme/myapp", pr_number=42)

    import time as _time

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=object())

        r1 = client.post("/api/inbox/acme/myapp/42/configure-and-pull-down")
        assert r1.status_code == 200
        session_id = r1.json()["session_id"]

        r2 = client.post(
            "/api/repos/onboard/complete",
            json={
                "session_id": session_id,
                "proposed_entry": {
                    "name": "myapp",
                    "path": str(repo_path),
                    "default_branch": "main",
                    "setup_steps": [],
                    "ticket_pattern": None,
                    "github_repo": "acme/myapp",
                },
            },
        )
        assert r2.status_code == 200, r2.text

        for _ in range(20):
            client.get("/api/health")
            if "args" in pull_down_call:
                break
            _time.sleep(0.05)

    assert pull_down_call.get("args") == ("acme/myapp", 42)
    assert repos_route._sessions[session_id].state == "saved"
