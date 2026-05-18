"""Tests for the pr_state service and refresh endpoint."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import gh_cli, pr_state
from app.services.pr_state import (
    BOT_LOGIN_PATTERN,
    PrChecks,
    PrComments,
    PrSummary,
    _classify,
    _compute_labels,
    _count_checks,
    _count_comments,
    get_pr_state_sync,
    summarize_gh_payload,
    upsert_pr_state_sync,
)


@pytest.fixture
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    config_path.write_text(
        yaml.safe_dump({"development_root": str(dev_root), "repos": []})
    )
    return {"db_path": db_path, "dev_root": dev_root, "config_path": config_path}


# --- bot regex --------------------------------------------------------------


def test_bot_pattern_matches_known_actors() -> None:
    for login in [
        "dependabot[bot]",
        "renovate-bot",
        "github-actions[bot]",
        "bugbot",
        "codecov-commenter",
        "Copilot",
        "datadog-checker",
        "cursor-agent",
        "semgrep-app",
    ]:
        assert BOT_LOGIN_PATTERN.search(login), login


def test_bot_pattern_does_not_match_humans() -> None:
    for login in ["alice", "octocat", "tylerjohn", "ada-lovelace"]:
        assert not BOT_LOGIN_PATTERN.search(login), login


# --- classify priority order ------------------------------------------------


def _c(passed: int = 0, fail: int = 0, pending: int = 0) -> PrChecks:
    return PrChecks(passed=passed, fail=fail, pending=pending, total=passed + fail + pending)


def _cm(human: int = 0, bot: int = 0) -> PrComments:
    return PrComments(human=human, bot=bot, total=human + bot)


def test_classify_merged_state_wins_over_everything() -> None:
    # MERGED is terminal — beats CI fail, conflicts, comments, etc.
    headline = _classify(
        state="MERGED",
        is_draft=False,
        mergeable=None,
        merge_state_status=None,
        review_decision="APPROVED",
        checks=_c(passed=5, fail=2),
        comments=_cm(human=3),
    )
    assert headline == "merged"


def test_classify_closed_state() -> None:
    headline = _classify(
        state="CLOSED",
        is_draft=False,
        mergeable=None,
        merge_state_status=None,
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(),
    )
    assert headline == "closed"


def test_classify_ci_failing_wins_over_everything() -> None:
    # Failing checks beat conflicts, approval, comments, draft, etc.
    headline = _classify(
        state="OPEN",
        is_draft=True,
        mergeable="CONFLICTING",
        merge_state_status="DIRTY",
        review_decision="APPROVED",
        checks=_c(passed=5, fail=1),
        comments=_cm(human=3, bot=2),
    )
    assert headline == "ci_failing"


def test_classify_merge_conflicts() -> None:
    headline = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="CONFLICTING",
        merge_state_status="DIRTY",
        review_decision="APPROVED",
        checks=_c(passed=5),
        comments=_cm(),
    )
    assert headline == "merge_conflicts"


def test_classify_ready_to_merge_needs_approved_and_green() -> None:
    # Approved but still pending checks → NOT ready_to_merge
    h1 = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="APPROVED",
        checks=_c(passed=3, pending=1),
        comments=_cm(),
    )
    assert h1 == "checks_running"

    # Approved AND green → ready_to_merge
    h2 = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="APPROVED",
        checks=_c(passed=12),
        comments=_cm(),
    )
    assert h2 == "ready_to_merge"


def test_classify_human_comment_only_when_not_approved_and_no_fails() -> None:
    # Human comment present, not approved, no fails → human_comment
    h = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(human=1, bot=3),
    )
    assert h == "human_comment"


def test_classify_human_comment_suppressed_when_approved() -> None:
    # Approved + green still wins even if humans commented.
    h = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision="APPROVED",
        checks=_c(passed=5),
        comments=_cm(human=2),
    )
    assert h == "ready_to_merge"


def test_classify_bot_only_comments_do_not_trigger_human_comment() -> None:
    h = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(bot=10),
    )
    # No human comment → fall through to waiting_on_others (or draft, but draft is False)
    assert h == "waiting_on_others"


def test_classify_checks_running() -> None:
    h = _classify(
        state="OPEN",
        is_draft=False,
        mergeable=None,
        merge_state_status=None,
        review_decision=None,
        checks=_c(passed=2, pending=3),
        comments=_cm(),
    )
    assert h == "checks_running"


def test_classify_draft_when_nothing_else() -> None:
    h = _classify(
        state="OPEN",
        is_draft=True,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(),
    )
    assert h == "draft"


def test_classify_waiting_on_others_is_default() -> None:
    h = _classify(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(),
    )
    assert h == "waiting_on_others"


# --- multi-label emission ---------------------------------------------------


def test_compute_labels_emits_multiple_signals() -> None:
    """A PR with failing CI AND a human comment surfaces BOTH labels —
    unlike the single-headline classifier which used to suppress the
    comment under the louder ci_failing signal."""
    labels = _compute_labels(
        state=None,
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=2, fail=1),
        comments=_cm(human=1),
    )
    assert "ci_failing" in labels
    assert "human_comment" in labels


def test_compute_labels_terminal_state_leads_priority_order() -> None:
    """Merged + ci_failing co-occur, but ``merged`` lands at index 0
    so the back-compat headline + tier mapping stay correct."""
    labels = _compute_labels(
        state="MERGED",
        is_draft=False,
        mergeable=None,
        merge_state_status=None,
        review_decision="APPROVED",
        checks=_c(passed=5, fail=1),
        comments=_cm(human=2),
    )
    assert labels[0] == "merged"
    assert "ci_failing" in labels


def test_compute_labels_falls_back_to_waiting_on_others() -> None:
    labels = _compute_labels(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(),
    )
    assert labels == ["waiting_on_others"]


def test_summarize_gh_payload_populates_labels() -> None:
    """End-to-end through the public summarizer: labels list + headline
    are both present and consistent."""
    summary = summarize_gh_payload(
        {
            "number": 1,
            "url": "https://x",
            "title": "t",
            "isDraft": False,
            "state": "OPEN",
            "statusCheckRollup": [{"bucket": "fail"}],
            "comments": [{"author": {"login": "alice"}}],
        }
    )
    assert summary.headline == "ci_failing"
    assert summary.labels[0] == "ci_failing"
    assert "human_comment" in summary.labels


def test_compute_labels_emits_unresolved_comments_when_threads_open() -> None:
    """A PR with 2 unresolved review threads gets the
    ``unresolved_comments`` label, ranked above ``human_comment``."""
    labels = _compute_labels(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(human=1),
        unresolved_threads=2,
    )
    assert "unresolved_comments" in labels
    assert "human_comment" in labels
    # Priority: unresolved_comments before human_comment.
    assert labels.index("unresolved_comments") < labels.index("human_comment")


def test_compute_labels_skips_unresolved_when_count_zero() -> None:
    labels = _compute_labels(
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        review_decision=None,
        checks=_c(passed=5),
        comments=_cm(),
        unresolved_threads=0,
    )
    assert "unresolved_comments" not in labels


def test_summarize_passes_unresolved_threads_through() -> None:
    """The count flows from the caller into PrSummary + label list."""
    summary = summarize_gh_payload(
        {
            "number": 1,
            "url": "https://x",
            "title": "t",
            "isDraft": False,
            "state": "OPEN",
            "statusCheckRollup": [{"bucket": "pass"}],
            "comments": [],
        },
        unresolved_threads=3,
    )
    assert summary.unresolved_threads == 3
    assert "unresolved_comments" in summary.labels


# --- check + comment counters ----------------------------------------------


def test_count_checks_uses_bucket_when_present() -> None:
    roll = [
        {"name": "test", "bucket": "pass"},
        {"name": "lint", "bucket": "fail"},
        {"name": "build", "bucket": "pending"},
        {"name": "other", "bucket": "pass"},
    ]
    out = _count_checks(roll)
    assert out.passed == 2
    assert out.fail == 1
    assert out.pending == 1
    assert out.total == 4


def test_count_checks_synthesizes_bucket_from_conclusion_or_state() -> None:
    roll = [
        {"name": "a", "conclusion": "SUCCESS"},   # gh sometimes uppercases
        {"name": "b", "conclusion": "FAILURE"},
        {"name": "c", "status": "IN_PROGRESS"},
        {"name": "d", "state": "success"},  # legacy commit-status form
    ]
    out = _count_checks(roll)
    assert out.passed == 2
    assert out.fail == 1
    assert out.pending == 1


def test_count_comments_classifies_bot_vs_human() -> None:
    comments = [
        {"author": {"login": "alice"}},
        {"author": {"login": "dependabot[bot]"}},
        {"author": {"login": "octocat"}},
        {"author": {"login": "github-actions[bot]"}},
    ]
    out = _count_comments(comments)
    assert out.human == 2
    assert out.bot == 2
    assert out.total == 4


# --- summarize_gh_payload --------------------------------------------------


def test_summarize_no_payload_is_no_pr() -> None:
    assert summarize_gh_payload(None).headline == "no_pr"
    assert summarize_gh_payload({}).headline == "no_pr"


def test_summarize_merged_pr_does_not_classify_as_ready() -> None:
    """Regression: a merged PR's gh payload looks otherwise like
    `ready_to_merge` (approved + green checks). Without checking
    `state`, the badge would mislabel a finished PR as "ready"."""
    summary = summarize_gh_payload(
        {
            "number": 88,
            "url": "https://github.com/acme/repo/pull/88",
            "title": "Already shipped",
            "state": "MERGED",
            "isDraft": False,
            "mergeable": None,
            "mergeStateStatus": None,
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [{"bucket": "pass"}, {"bucket": "pass"}],
            "comments": [],
            "baseRefName": "main",
            "headRefName": "feat/old",
            "updatedAt": "2026-05-12T11:00:00Z",
        }
    )
    assert summary.headline == "merged"


def test_summarize_end_to_end() -> None:
    summary = summarize_gh_payload(
        {
            "number": 42,
            "url": "https://github.com/acme/repo/pull/42",
            "title": "Add carrier filter",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [{"bucket": "pass"}, {"bucket": "pass"}],
            "comments": [
                {"author": {"login": "alice"}},
                {"author": {"login": "dependabot[bot]"}},
            ],
            "baseRefName": "main",
            "headRefName": "feat/x",
            "updatedAt": "2026-05-13T11:00:00Z",
        }
    )
    assert summary.headline == "ready_to_merge"
    assert summary.pr_number == 42
    assert summary.url == "https://github.com/acme/repo/pull/42"
    assert summary.checks.passed == 2
    assert summary.checks.fail == 0
    assert summary.comments.human == 1
    assert summary.comments.bot == 1


# --- upsert + read round-trip ---------------------------------------------


def _seed_worktree(db_path: Path, repo: str, name: str, dev_root: Path) -> None:
    wt_path = dev_root / name
    wt_path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo, name, str(wt_path), "main", "2026-01-01T00:00:00Z", "ready"),
        )
        conn.commit()
    finally:
        conn.close()


def test_upsert_and_get_pr_state(_isolate: dict[str, Path]) -> None:
    _seed_worktree(_isolate["db_path"], "myrepo", "feature1", _isolate["dev_root"])
    summary = PrSummary(
        headline="ready_to_merge",
        pr_number=42,
        url="https://github.com/acme/repo/pull/42",
        title="hi",
        checks=PrChecks(passed=3, total=3),
        comments=PrComments(human=1, bot=0, total=1),
    )
    upsert_pr_state_sync("myrepo", "feature1", summary, db_path=_isolate["db_path"])
    fetched = get_pr_state_sync("myrepo", "feature1", db_path=_isolate["db_path"])
    assert fetched is not None
    assert fetched.headline == "ready_to_merge"
    assert fetched.payload["pr_number"] == 42
    assert fetched.payload["checks"]["passed"] == 3


def test_upsert_replaces_existing_row(_isolate: dict[str, Path]) -> None:
    _seed_worktree(_isolate["db_path"], "myrepo", "feature1", _isolate["dev_root"])
    s1 = PrSummary(headline="checks_running", pr_number=1)
    s2 = PrSummary(headline="ready_to_merge", pr_number=1)
    upsert_pr_state_sync("myrepo", "feature1", s1, db_path=_isolate["db_path"])
    upsert_pr_state_sync("myrepo", "feature1", s2, db_path=_isolate["db_path"])
    fetched = get_pr_state_sync("myrepo", "feature1", db_path=_isolate["db_path"])
    assert fetched.headline == "ready_to_merge"


# --- fetch_pr_summary (gh subprocess mocked) -------------------------------


def test_fetch_pr_summary_parses_gh_json(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    """Stub asyncio.create_subprocess_exec to return a canned `gh pr view`
    response and confirm we map it into a PrSummary."""
    gh_json = json.dumps(
        {
            "number": 7,
            "url": "https://github.com/x/y/pull/7",
            "title": "t",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": None,
            "statusCheckRollup": [{"bucket": "pass"}, {"bucket": "pending"}],
            "comments": [{"author": {"login": "alice"}}],
            "baseRefName": "main",
            "headRefName": "feat/y",
            "updatedAt": "2026-05-13T10:00:00Z",
        }
    ).encode()

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(gh_json, b""))
    fake_proc.returncode = 0
    monkeypatch.setattr(
        gh_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc)
    )

    summary = asyncio.run(pr_state.fetch_pr_summary(_isolate["dev_root"]))
    # Human comment + not approved + no failing checks beats
    # checks_running in the priority order (matches the skill).
    assert summary.headline == "human_comment"
    assert summary.pr_number == 7
    assert summary.comments.human == 1


def test_fetch_pr_summary_returns_no_pr_on_gh_not_found(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"no pull requests found for branch foo"))
    fake_proc.returncode = 1
    monkeypatch.setattr(
        gh_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc)
    )
    summary = asyncio.run(pr_state.fetch_pr_summary(_isolate["dev_root"]))
    assert summary.headline == "no_pr"
    assert summary.pr_number is None


def test_fetch_pr_summary_raises_on_gh_missing(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"command not found: gh"))
    fake_proc.returncode = 127
    monkeypatch.setattr(
        gh_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc)
    )
    with pytest.raises(gh_cli.GhNotFound):
        asyncio.run(pr_state.fetch_pr_summary(_isolate["dev_root"]))


def _thread(
    *,
    resolved: bool,
    outdated: bool,
    last_author: str | None,
) -> dict:
    comments_nodes = (
        [{"author": {"login": last_author}}] if last_author is not None else []
    )
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": comments_nodes},
    }


def test_fetch_unresolved_threads_count_parses_graphql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counts reviewThreads that are unresolved AND un-outdated AND
    whose last comment isn't from the PR author. Resolved threads,
    outdated threads, and threads whose last reply is the author's
    own all drop out."""
    graphql_json = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "author": {"login": "octocat"},
                        "reviewThreads": {
                            "nodes": [
                                # count — reviewer last
                                _thread(resolved=False, outdated=False, last_author="reviewer1"),
                                # count — reviewer last
                                _thread(resolved=False, outdated=False, last_author="reviewer2"),
                                # skip — author replied last
                                _thread(resolved=False, outdated=False, last_author="octocat"),
                                # skip — resolved
                                _thread(resolved=True, outdated=False, last_author="reviewer1"),
                                # skip — outdated
                                _thread(resolved=False, outdated=True, last_author="reviewer1"),
                            ]
                        },
                    }
                }
            }
        }
    ).encode()

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(graphql_json, b""))
    fake_proc.returncode = 0
    monkeypatch.setattr(
        gh_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc)
    )

    count = asyncio.run(
        pr_state._fetch_unresolved_threads_count("o", "r", 42)
    )
    assert count == 2


def test_fetch_unresolved_threads_count_falls_back_when_author_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the PR author can't be resolved (deleted GitHub account, weird
    payload), we fall back to counting every unresolved+un-outdated
    thread rather than silently under-counting."""
    graphql_json = json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "author": None,
                        "reviewThreads": {
                            "nodes": [
                                _thread(resolved=False, outdated=False, last_author="octocat"),
                                _thread(resolved=False, outdated=False, last_author="reviewer1"),
                                _thread(resolved=True, outdated=False, last_author="reviewer1"),
                            ]
                        },
                    }
                }
            }
        }
    ).encode()

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(graphql_json, b""))
    fake_proc.returncode = 0
    monkeypatch.setattr(
        gh_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc)
    )

    count = asyncio.run(
        pr_state._fetch_unresolved_threads_count("o", "r", 42)
    )
    assert count == 2


def test_fetch_unresolved_threads_count_zero_on_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open: any gh hiccup returns 0 rather than crashing the
    summary fetch."""
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b"some random gh error"))
    fake_proc.returncode = 1
    monkeypatch.setattr(
        gh_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake_proc)
    )
    count = asyncio.run(
        pr_state._fetch_unresolved_threads_count("o", "r", 42)
    )
    assert count == 0


# --- /api/worktree/.../pr-state/refresh endpoint ----------------------------


def test_pr_state_refresh_endpoint(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    _seed_worktree(_isolate["db_path"], "myrepo", "feature1", _isolate["dev_root"])

    # Stub the actual gh call so the test is offline-safe.
    async def fake_fetch(path: Path) -> PrSummary:
        return PrSummary(
            headline="ready_to_merge",
            pr_number=99,
            url="https://github.com/x/y/pull/99",
            title="ready",
            checks=PrChecks(passed=4, total=4),
        )

    monkeypatch.setattr(pr_state, "fetch_pr_summary", fake_fetch)

    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post("/api/worktree/myrepo/feature1/pr-state/refresh")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["headline"] == "ready_to_merge"
    assert body["pr_number"] == 99
    assert body["checks"]["passed"] == 4
    assert "checked_at" in body

    # Row written to DB
    fetched = get_pr_state_sync("myrepo", "feature1", db_path=_isolate["db_path"])
    assert fetched is not None
    assert fetched.headline == "ready_to_merge"


def test_pr_state_refresh_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        client.app.state.iterm = SimpleNamespace(connection=None)
        r = client.post("/api/worktree/myrepo/missing/pr-state/refresh")
    assert r.status_code == 404


# --- list-worktrees response includes pr_state from join -------------------


def test_list_worktrees_includes_pr_state(_isolate: dict[str, Path]) -> None:
    _seed_worktree(_isolate["db_path"], "myrepo", "feature1", _isolate["dev_root"])
    upsert_pr_state_sync(
        "myrepo",
        "feature1",
        PrSummary(
            headline="ci_failing",
            pr_number=1,
            url="https://github.com/x/y/pull/1",
            checks=PrChecks(passed=2, fail=1, total=3),
        ),
        db_path=_isolate["db_path"],
    )

    with TestClient(app) as client:
        r = client.get("/api/worktrees")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["pr_state"] is not None
    assert rows[0]["pr_state"]["headline"] == "ci_failing"
    assert rows[0]["pr_state"]["checks"]["fail"] == 1


def test_list_worktrees_handles_missing_pr_state(_isolate: dict[str, Path]) -> None:
    """Worktrees that haven't been polled yet should still serialize cleanly
    with pr_state: None — the LEFT JOIN returns NULLs we have to handle."""
    _seed_worktree(_isolate["db_path"], "myrepo", "nopr", _isolate["dev_root"])
    with TestClient(app) as client:
        r = client.get("/api/worktrees")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["pr_state"] is None
