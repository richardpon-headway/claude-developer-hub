"""Tests for the read-only inbox slice (slice 1)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.config.schema import CDHConfig, InboxConfig
from app.main import app
from app.services import inbox_poll, inbox_search
from app.services.inbox_poll import InboxCache, InboxPr
from app.services.inbox_search import (
    InboxPrRaw,
    _ci_status_from_rollup,
    _row_from_gh,
    configured_repos_index,
    is_repo_configured,
)
from app.services.inbox_stack import annotate_stacks

# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    return {"db_path": db_path, "config_path": config_path}


def _write_minimal_config(config_path: Path, teams: list[str] | None = None) -> None:
    cfg: dict[str, Any] = {"repos": []}
    if teams is not None:
        cfg["inbox"] = {"teams": teams}
    config_path.write_text(yaml.safe_dump(cfg))


def _raw(
    *,
    repo: str,
    number: int,
    head: str,
    base: str = "main",
    source: str = "author",
    title: str | None = None,
) -> InboxPrRaw:
    return InboxPrRaw(
        pr_repo=repo,
        pr_number=number,
        title=title or f"PR #{number}",
        author_login="me",
        head_ref=head,
        base_ref=base,
        is_draft=False,
        url=f"https://github.com/{repo}/pull/{number}",
        updated_at="2026-05-14T00:00:00Z",
        ci_status="pass",
        source=source,
    )


# --- config schema -------------------------------------------------------


def test_inbox_config_default_empty_teams() -> None:
    cfg = CDHConfig()
    assert cfg.inbox.teams == []


def test_inbox_config_team_validation_rejects_bad_slugs() -> None:
    with pytest.raises(Exception) as exc_info:
        InboxConfig(teams=["just-team-name"])
    assert "owner/team" in str(exc_info.value)


def test_inbox_config_accepts_owner_team_format() -> None:
    cfg = InboxConfig(teams=["headway/corrections", "acme/build-team_1"])
    assert cfg.teams == ["headway/corrections", "acme/build-team_1"]


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
        "headRefName": "feat/x",
        "baseRefName": "main",
        "repository": {"name": "r", "owner": {"login": "o"}},
        "statusCheckRollup": [{"state": "SUCCESS"}],
    }
    row = _row_from_gh(entry, source="author")
    assert row is not None
    assert row.pr_repo == "o/r"
    assert row.pr_number == 42
    assert row.head_ref == "feat/x"
    assert row.ci_status == "pass"
    assert row.source == "author"


def test_row_from_gh_returns_none_on_missing_fields() -> None:
    assert _row_from_gh({}, source="author") is None
    # number present but no head/base ref
    assert _row_from_gh(
        {
            "number": 1,
            "title": "t",
            "url": "https://x",
            "repository": {"name": "r", "owner": {"login": "o"}},
        },
        source="author",
    ) is None


# --- stack annotation ----------------------------------------------------


def test_stack_annotation_single_pr() -> None:
    prs = [_raw(repo="o/r", number=1, head="feat/a")]
    ann = annotate_stacks(prs)
    a = ann[("o/r", 1)]
    assert a.stack_size == 1
    assert a.stack_position == 1
    assert a.stack_top_pr_number is None


def test_stack_annotation_three_pr_chain() -> None:
    # Stack: A (head=feat/a, base=main) → B (head=feat/b, base=feat/a)
    # → C (head=feat/c, base=feat/b). C is the top.
    prs = [
        _raw(repo="o/r", number=1, head="feat/a", base="main"),
        _raw(repo="o/r", number=2, head="feat/b", base="feat/a"),
        _raw(repo="o/r", number=3, head="feat/c", base="feat/b"),
    ]
    ann = annotate_stacks(prs)
    assert ann[("o/r", 1)].stack_top_pr_number == 3
    assert ann[("o/r", 1)].stack_size == 3
    assert ann[("o/r", 1)].stack_position == 1  # bottom (closest to main)
    assert ann[("o/r", 2)].stack_position == 2
    assert ann[("o/r", 3)].stack_position == 3  # top


def test_stack_annotation_does_not_cross_repos() -> None:
    # A PR in repo X with base_ref matching a head_ref in repo Y must
    # NOT form a stack — stacks are repo-local.
    prs = [
        _raw(repo="o/x", number=1, head="feat/shared"),
        _raw(repo="o/y", number=2, head="feat/top", base="feat/shared"),
    ]
    ann = annotate_stacks(prs)
    assert ann[("o/x", 1)].stack_size == 1
    assert ann[("o/y", 2)].stack_size == 1


# --- repo configuration matching ---------------------------------------


def test_is_repo_configured_matches_on_basename_fallback() -> None:
    """When ``github_repo`` isn't set, fall back to matching the basename
    portion of ``pr_repo`` against ``RepoConfig.name``."""
    from app.config.schema import RepoConfig

    repos = [RepoConfig(name="myapp", path=Path("/tmp/myapp"))]
    idx = configured_repos_index(repos)
    assert is_repo_configured("acme/myapp", idx) is True
    assert is_repo_configured("acme/other", idx) is False
    assert is_repo_configured("just-a-string", idx) is False


def test_explicit_github_repo_excludes_basename_collisions() -> None:
    """When ``github_repo`` is set, only the explicit owner/name matches.
    A different-owner PR with the same basename does NOT piggy-back on
    the basename fallback — the user opted into precision."""
    from app.config.schema import RepoConfig
    from app.services.inbox_search import lookup_configured_repo

    repos = [
        RepoConfig(
            name="myapp",
            path=Path("/tmp/myapp"),
            github_repo="headway/myapp",
        )
    ]
    idx = configured_repos_index(repos)
    assert lookup_configured_repo("headway/myapp", idx) is not None
    assert lookup_configured_repo("acme/myapp", idx) is None
    assert lookup_configured_repo("headway/other", idx) is None


# --- dedup pull from worktree + pr_state --------------------------------


def _seed_worktree_row(
    db_path: Path,
    repo: str,
    name: str,
    *,
    pr_repo: str | None = None,
    pr_number: int | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, created_at, status, "
            "pr_number, pr_repo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo,
                name,
                f"/tmp/{repo}_{name}",
                "feat/x",
                "2026-05-14T00:00:00Z",
                "ready",
                pr_number,
                pr_repo,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_pr_state(
    db_path: Path, repo: str, name: str, pr_number: int
) -> None:
    import json

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO pr_state (repo, worktree_name, headline, payload, checked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                repo,
                name,
                "ready_to_merge",
                json.dumps({"pr_number": pr_number}),
                "2026-05-14T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_tracked_keys_reads_from_worktree_columns(_isolate: dict[str, Path]) -> None:
    _seed_worktree_row(
        _isolate["db_path"], "myapp", "feat1", pr_repo="o/myapp", pr_number=7
    )
    keys = inbox_poll._tracked_pr_keys_sync()
    assert keys == {("o/myapp", 7)}


def test_tracked_keys_reads_from_pr_state_too(_isolate: dict[str, Path]) -> None:
    # Worktree has pr_repo set (so we can join to recover owner/name)
    # but pr_number is NULL — pr_state should still surface a match.
    _seed_worktree_row(
        _isolate["db_path"], "myapp", "feat1", pr_repo="o/myapp", pr_number=None
    )
    _seed_pr_state(_isolate["db_path"], "myapp", "feat1", pr_number=99)
    keys = inbox_poll._tracked_pr_keys_sync()
    assert keys == {("o/myapp", 99)}


# --- endpoint ------------------------------------------------------------


def test_endpoint_returns_empty_before_first_poll(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        # Force the cache to a known-empty state (lifespan would have
        # initialized it via the poll loop; we replace it here).
        client.app.state.inbox = InboxCache()
        r = client.get("/api/inbox")
    assert r.status_code == 200
    body = r.json()
    assert body["prs"] == []
    assert body["checked_at"] is None


def test_endpoint_returns_cached_after_poll(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"])
    cached = InboxCache(
        prs=[
            InboxPr(
                pr_repo="o/r",
                pr_number=1,
                title="t",
                author_login="me",
                head_ref="feat/x",
                base_ref="main",
                is_draft=False,
                url="https://github.com/o/r/pull/1",
                updated_at="2026-05-14T00:00:00Z",
                ci_status="pass",
                source="author",
                stack_top_pr_number=None,
                stack_size=1,
                stack_position=1,
                repo_configured=False,
            )
        ],
        checked_at="2026-05-14T00:00:00Z",
    )
    with TestClient(app) as client:
        client.app.state.inbox = cached
        r = client.get("/api/inbox")
    body = r.json()
    assert len(body["prs"]) == 1
    assert body["prs"][0]["pr_repo"] == "o/r"
    assert body["prs"][0]["source"] == "author"
    assert body["checked_at"] == "2026-05-14T00:00:00Z"


def test_endpoint_when_state_has_no_inbox_attr(_isolate: dict[str, Path]) -> None:
    """If the route fires before the lifespan poller has initialized
    the cache, return empty rather than 500."""
    _write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        # Remove the attribute that lifespan set up.
        if hasattr(client.app.state, "inbox"):
            delattr(client.app.state, "inbox")
        r = client.get("/api/inbox")
    assert r.status_code == 200
    assert r.json() == {"prs": [], "checked_at": None}


# --- source attribution + filter ----------------------------------------


def test_source_attribution_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR returned by both author + team queries gets source='author'."""

    call_count = {"n": 0}

    async def fake_search(query: str, *, source: str) -> list[InboxPrRaw]:
        call_count["n"] += 1
        if "author:@me" in query:
            return [_raw(repo="o/r", number=42, head="feat/x", source="author")]
        if "review-requested:@me" in query:
            return []
        if "team-review-requested:" in query:
            # Same PR — should be filtered out by the priority dedup
            return [_raw(repo="o/r", number=42, head="feat/x", source=source)]
        return []

    monkeypatch.setattr(inbox_search, "_search", fake_search)

    import asyncio

    result = asyncio.run(inbox_search.fetch_inbox_prs(["headway/corrections"]))
    assert len(result) == 1
    assert result[0].source == "author"
    assert call_count["n"] == 3  # author + reviewer + 1 team


def test_filter_out_worktree_prs_drops_matches() -> None:
    prs = [
        _raw(repo="o/r", number=1, head="feat/a"),
        _raw(repo="o/r", number=2, head="feat/b"),
    ]
    out = inbox_search.filter_out_worktree_prs(prs, tracked={("o/r", 1)})
    assert [p.pr_number for p in out] == [2]


# --- poll tick end-to-end with mocks ------------------------------------


def test_tick_populates_cache(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_minimal_config(_isolate["config_path"])

    raw_rows = [_raw(repo="o/r", number=42, head="feat/x")]

    async def fake_fetch(teams: list[str]) -> list[InboxPrRaw]:
        return raw_rows

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace(inbox=InboxCache())
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    assert state.inbox.checked_at is not None
    assert len(state.inbox.prs) == 1
    assert state.inbox.prs[0].pr_number == 42


def test_tick_dedup_against_worktree(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_minimal_config(_isolate["config_path"])
    _seed_worktree_row(
        _isolate["db_path"], "myapp", "feat1", pr_repo="o/r", pr_number=42
    )

    raw_rows = [
        _raw(repo="o/r", number=42, head="feat/x"),
        _raw(repo="o/r", number=43, head="feat/y"),
    ]

    async def fake_fetch(teams: list[str]) -> list[InboxPrRaw]:
        return raw_rows

    monkeypatch.setattr(inbox_poll, "fetch_inbox_prs", fake_fetch)

    state = SimpleNamespace(inbox=InboxCache())
    import asyncio

    asyncio.run(inbox_poll._tick(state))

    # #42 dropped (already a worktree), #43 surfaces.
    assert [p.pr_number for p in state.inbox.prs] == [43]


# --- POST /api/inbox/.../pull-down --------------------------------------


def _seed_inbox_cache(*prs: InboxPr) -> InboxCache:
    return InboxCache(prs=list(prs), checked_at="2026-05-14T00:00:00Z")


def _enriched(
    *,
    pr_repo: str,
    pr_number: int,
    repo_configured: bool = True,
    head_ref: str = "feat/x",
) -> InboxPr:
    return InboxPr(
        pr_repo=pr_repo,
        pr_number=pr_number,
        title=f"PR #{pr_number}",
        author_login="me",
        head_ref=head_ref,
        base_ref="main",
        is_draft=False,
        url=f"https://github.com/{pr_repo}/pull/{pr_number}",
        updated_at="2026-05-14T00:00:00Z",
        ci_status="pass",
        source="author",
        stack_top_pr_number=None,
        stack_size=1,
        stack_position=1,
        repo_configured=repo_configured,
    )


def _write_repo_config(
    config_path: Path,
    *,
    repo_name: str,
    repo_path: Path,
    github_repo: str | None = None,
    development_root: Path | None = None,
) -> None:
    entry: dict[str, Any] = {
        "name": repo_name,
        "path": str(repo_path),
        "default_branch": "main",
        "setup_steps": [],
        "ticket_pattern": None,
    }
    if github_repo is not None:
        entry["github_repo"] = github_repo
    cfg: dict[str, Any] = {"repos": [entry]}
    if development_root is not None:
        cfg["development_root"] = str(development_root)
    config_path.write_text(yaml.safe_dump(cfg))


def test_pull_down_404_when_pr_not_in_cache(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        client.app.state.inbox = _seed_inbox_cache()
        r = client.post("/api/inbox/o/r/42/pull-down")
    assert r.status_code == 404


def test_pull_down_400_when_repo_not_configured(_isolate: dict[str, Path]) -> None:
    _write_minimal_config(_isolate["config_path"])
    with TestClient(app) as client:
        client.app.state.inbox = _seed_inbox_cache(
            _enriched(pr_repo="acme/other", pr_number=42, repo_configured=False)
        )
        r = client.post("/api/inbox/acme/other/42/pull-down")
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_pull_down_400_when_repo_path_missing_on_disk(
    _isolate: dict[str, Path],
) -> None:
    bogus = _isolate["db_path"].parent / "nope"
    _write_repo_config(
        _isolate["config_path"],
        repo_name="myapp",
        repo_path=bogus,
        github_repo="acme/myapp",
    )
    with TestClient(app) as client:
        client.app.state.inbox = _seed_inbox_cache(
            _enriched(pr_repo="acme/myapp", pr_number=42)
        )
        r = client.post("/api/inbox/acme/myapp/42/pull-down")
    assert r.status_code == 400
    assert "missing on disk" in r.json()["detail"]


def test_pull_down_same_repo_happy_path(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-repo PR: no pre-fetch needed; create_worktree's built-in
    fetch handles it. Verify the worktree is created with the head_ref
    branch and the pr_number/pr_repo columns are populated."""
    import subprocess

    # Init a real local repo so create_worktree's git invocations work
    # against a non-mock filesystem.
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
    _write_repo_config(
        _isolate["config_path"],
        repo_name="myapp",
        repo_path=repo_path,
        github_repo="acme/myapp",
        development_root=tmp_path,
    )

    # Mock `gh pr view` (same-repo)
    from app.routes import inbox as inbox_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/x", "isCrossRepository": False}

    monkeypatch.setattr(inbox_route, "run_gh_json", fake_run_gh_json)

    fetch_called = {"n": 0}

    async def fake_fetch_pr_ref(*a: Any, **kw: Any) -> None:
        fetch_called["n"] += 1

    monkeypatch.setattr(inbox_route, "_fetch_pr_ref", fake_fetch_pr_ref)

    with TestClient(app) as client:
        client.app.state.inbox = _seed_inbox_cache(
            _enriched(pr_repo="acme/myapp", pr_number=42, head_ref="feat/x")
        )
        r = client.post("/api/inbox/acme/myapp/42/pull-down")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo"] == "myapp"
    # Same-repo: no fork-ref fetch
    assert fetch_called["n"] == 0

    # pr_number + pr_repo persisted on the new worktree row
    conn = sqlite3.connect(_isolate["db_path"])
    try:
        row = conn.execute(
            "SELECT pr_number, pr_repo FROM worktree WHERE repo=?",
            ("myapp",),
        ).fetchone()
    finally:
        conn.close()
    assert row == (42, "acme/myapp")


def test_pull_down_fork_pr_fetches_pull_ref(
    _isolate: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork PR: must pre-fetch refs/pull/<n>/head into a local branch
    before create_worktree runs (otherwise verify-remote fails since
    the head ref doesn't live on origin)."""
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
    _write_repo_config(
        _isolate["config_path"],
        repo_name="myapp",
        repo_path=repo_path,
        github_repo="acme/myapp",
        development_root=tmp_path,
    )

    from app.routes import inbox as inbox_route

    async def fake_run_gh_json(args: list, **kwargs: Any) -> dict:
        return {"headRefName": "feat/forked", "isCrossRepository": True}

    fetch_args_seen: list[tuple[Any, ...]] = []

    async def fake_fetch_pr_ref(repo_p: Path, pr_n: int, local_b: str) -> None:
        fetch_args_seen.append((repo_p, pr_n, local_b))
        # Simulate the fork-ref fetch creating the local branch (so
        # create_worktree's verify-local step passes). Async-spawned
        # to keep ruff's ASYNC221 happy.
        import asyncio as _asyncio

        proc = await _asyncio.create_subprocess_exec(
            "git", "-C", str(repo_p), "branch", local_b,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    monkeypatch.setattr(inbox_route, "run_gh_json", fake_run_gh_json)
    monkeypatch.setattr(inbox_route, "_fetch_pr_ref", fake_fetch_pr_ref)

    with TestClient(app) as client:
        client.app.state.inbox = _seed_inbox_cache(
            _enriched(pr_repo="acme/myapp", pr_number=58, head_ref="feat/forked")
        )
        r = client.post("/api/inbox/acme/myapp/58/pull-down")

    assert r.status_code == 200, r.text
    # Pre-fetch was invoked with the expected branch name
    assert len(fetch_args_seen) == 1
    _, pr_n, local_b = fetch_args_seen[0]
    assert pr_n == 58
    assert local_b == "cdh-pr-58-feat/forked"
