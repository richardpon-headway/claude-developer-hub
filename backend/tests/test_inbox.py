"""Tests for the persistent-inbox slice."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config.schema import CDHConfig, InboxConfig
from app.main import app
from app.models.pr import PrRow
from app.services import inbox_poll, inbox_search, pr_db
from app.services.inbox_search import (
    InboxPrRaw,
    _ci_status_from_rollup,
    _row_from_gh,
    configured_repos_index,
    is_repo_configured,
)
from tests.fixtures.config import write_minimal_config, write_repo_config
from tests.fixtures.inbox import build_raw_pr, seed_inbox_row
from tests.fixtures.worktree import seed_worktree


def _list_inbox_rows(db_path: Path) -> list[PrRow]:
    """Pull the route-visible inbox surface directly from pr_db."""
    return pr_db.list_pr_sync(
        is_inbox=True,
        is_archived=False,
        is_bookmarked=False,
        has_worktree=False,
        order_by="pr.pr_updated_at DESC",
        db_path=db_path,
    )


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
    cfg = InboxConfig(teams=["corp/team_name", "acme/build-team_1"])
    assert cfg.teams == ["corp/team_name", "acme/build-team_1"]


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


# --- source accumulation across queries (auth dropped, team dropped) ----


def test_source_accumulation_across_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR returned by multiple queries accumulates all sources,
    priority-ordered by call order (reviewer > assignee > mentions)."""

    call_count = {"n": 0}
    queries_seen: list[str] = []

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        call_count["n"] += 1
        queries_seen.append(query)
        if "user-review-requested:@me" in query:
            return [build_raw_pr(repo="o/r", number=42, head="feat/x", source="reviewer")]
        if "mentions:@me" in query:
            return [build_raw_pr(repo="o/r", number=42, head="feat/x", source="mentions")]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    assert len(result) == 1
    assert result[0].sources == ["reviewer", "mentions"]
    # reviewer + assignee + mentions = 3 queries
    assert call_count["n"] == 3
    # The reviewer query uses the user-* variant so team-mediated
    # requests are filtered at the search layer.
    assert "user-review-requested:@me" in queries_seen


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

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs())
    by_number = {p.pr_number: p for p in result}
    assert by_number[100].sources == ["assignee"]
    assert by_number[101].sources == ["mentions"]


def test_reviewer_query_uses_user_review_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The team-mediated post-filter is gone; instead we rely on
    GitHub's ``user-review-requested:`` qualifier, which already
    excludes team-mediated requests at the search layer."""
    queries_seen: list[str] = []

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        queries_seen.append(query)
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    asyncio.run(inbox_search.fetch_inbox_prs())

    # MUST use the user-* form. The plain `review-requested:` form
    # would silently expand via team membership and pollute the inbox.
    assert "user-review-requested:@me" in queries_seen
    assert "review-requested:@me" not in queries_seen


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

    rows = _list_inbox_rows(_isolate["db_path"])
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

    rows = _list_inbox_rows(_isolate["db_path"])
    assert [(r.pr_repo, r.pr_number) for r in rows] == [("o/r", 43)]


def test_tick_skips_archived_rows(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archived PR re-appearing in gh search results must NOT re-enter
    the active inbox view."""
    write_minimal_config(_isolate["config_path"])
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=42, is_archived=True,
              archived_at="2026-05-14T00:00:00Z"),
        db_path=_isolate["db_path"],
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return [build_raw_pr(repo="o/r", number=42, head="feat/x")]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    # Archived row stays filtered from the active inbox list.
    assert _list_inbox_rows(_isolate["db_path"]) == []


def test_tick_auto_removes_closed_pr(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR that's no longer in gh search AND has pr.state set to
    'merged' (by the enrichment loop) gets pruned: inbox + archive
    flags cleared, row GC'd if no other surface holds it."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="o/r",
        pr_number=99,
        last_seen_at="2026-05-01T00:00:00Z",
    )
    # Simulate the enrichment loop having written pr.state='merged'.
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=99, state="merged"),
        db_path=_isolate["db_path"],
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return []  # PR no longer in search

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))
    assert _list_inbox_rows(_isolate["db_path"]) == []


def test_tick_keeps_still_open_stale_row(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-open PR that fell out of gh search results stays in
    the inbox — the sweep only removes rows whose pr.state is closed
    or merged."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(
        _isolate["db_path"],
        pr_repo="o/r",
        pr_number=99,
        last_seen_at="2026-05-01T00:00:00Z",
    )
    pr_db.upsert_pr_sync(
        PrRow(pr_repo="o/r", pr_number=99, state="open"),
        db_path=_isolate["db_path"],
    )

    async def fake_fetch() -> list[InboxPrRaw]:
        return []

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    rows = _list_inbox_rows(_isolate["db_path"])
    assert len(rows) == 1
    # last_seen_at is no longer bumped by the sweep (the enrichment
    # loop's state read replaced the per-row gh probe).
    assert rows[0].last_seen_at == "2026-05-01T00:00:00Z"


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
    rows = _list_inbox_rows(_isolate["db_path"])
    assert len(rows) == 1
    assert rows[0].ticket == "PROJ-218"


def test_tick_excludes_bookmarked_prs_from_inbox_list(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR that's both review-requested AND bookmarked stays out of
    the inbox surface — bookmark wins (explicit user pin). The
    discovery loop upserts both PRs into the unified pr table; the
    inbox list filter (`is_bookmarked=False`) is what enforces the
    surface precedence."""
    write_minimal_config(_isolate["config_path"])
    from tests.fixtures.bookmark import seed_bookmark

    seed_bookmark(_isolate["db_path"], pr_repo="o/r", pr_number=42)

    async def fake_fetch() -> list[InboxPrRaw]:
        return [
            build_raw_pr(repo="o/r", number=42, head="feat/x"),
            build_raw_pr(repo="o/r", number=43, head="feat/y"),
        ]

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace()
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    rows = _list_inbox_rows(_isolate["db_path"])
    assert [(r.pr_repo, r.pr_number) for r in rows] == [("o/r", 43)]


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
    pr_db.set_archived_flag_sync(
        "o/r", 2, True, archived_at="2026-05-14T00:00:00Z",
        db_path=_isolate["db_path"],
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


def test_archive_endpoint_404_on_repeat_archive(
    _isolate: dict[str, Path],
) -> None:
    """Archive clears ``is_inbox`` (the row vanishes from the surface),
    so a second archive call 404s. The pr_db-layer
    ``set_archived_flag_sync`` is still idempotent on ``archived_at``
    (see test_pr.py:test_set_archived_flag_is_idempotent_on_archived_at)."""
    write_minimal_config(_isolate["config_path"])
    seed_inbox_row(_isolate["db_path"], pr_repo="o/r", pr_number=1)
    with TestClient(app) as client:
        r1 = client.post("/api/inbox/o/r/1/archive")
        assert r1.status_code == 200
        r2 = client.post("/api/inbox/o/r/1/archive")
        assert r2.status_code == 404


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

    row = pr_db.get_pr_sync("o/r", 1, db_path=_isolate["db_path"])
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
    row = pr_db.get_pr_sync("o/r", 1, db_path=_isolate["db_path"])
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
            "SELECT w.pr_number, w.pr_repo, pr.author_login "
            "FROM worktree w "
            "LEFT JOIN pr "
            "  ON pr.pr_repo = w.pr_repo AND pr.pr_number = w.pr_number "
            "WHERE w.repo = ?",
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

    from app.routes import repos as repos_route
    from app.services import terminal as terminal_mod

    spawn_args_seen: dict[str, Any] = {}

    async def fake_spawn(request, cwd, initial_prompt):  # type: ignore[no-untyped-def]
        spawn_args_seen["cwd"] = cwd
        spawn_args_seen["prompt"] = initial_prompt
        return None

    monkeypatch.setattr(terminal_mod, "spawn_one_tab_claude", fake_spawn)

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
    from app.services import terminal as terminal_mod

    async def fake_spawn(*args: Any, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(terminal_mod, "spawn_one_tab_claude", fake_spawn)

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
