"""Tests for the Slice N worktree-import feature."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import worktree_import
from app.services.worktree_import import (
    parse_worktree_list_porcelain,
)
from tests.fixtures.config import write_minimal_config
from tests.fixtures.worktree import init_git_repo, make_worktree

# --- parser --------------------------------------------------------------


def test_parser_handles_typical_record() -> None:
    output = (
        "worktree /a/main\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /a/feature\n"
        "HEAD def456\n"
        "branch refs/heads/feature\n"
    )
    records = parse_worktree_list_porcelain(output)
    assert records == [
        {"worktree": "/a/main", "HEAD": "abc123", "branch": "refs/heads/main"},
        {"worktree": "/a/feature", "HEAD": "def456", "branch": "refs/heads/feature"},
    ]


def test_parser_recognizes_bare_and_detached_flags() -> None:
    output = (
        "worktree /a/bare\n"
        "bare\n"
        "\n"
        "worktree /a/detached\n"
        "HEAD abc123\n"
        "detached\n"
        "\n"
        "worktree /a/prunable\n"
        "HEAD def456\n"
        "branch refs/heads/x\n"
        "prunable reason\n"
    )
    records = parse_worktree_list_porcelain(output)
    assert records[0] == {"worktree": "/a/bare", "bare": True}
    assert records[1] == {
        "worktree": "/a/detached",
        "HEAD": "abc123",
        "detached": True,
    }
    assert records[2]["prunable"] == "reason"


def test_parser_handles_trailing_blank_lines() -> None:
    output = "worktree /a\nHEAD x\nbranch refs/heads/main\n\n\n\n"
    records = parse_worktree_list_porcelain(output)
    assert len(records) == 1


def test_parser_handles_no_trailing_newline() -> None:
    output = "worktree /a\nHEAD x\nbranch refs/heads/main"
    records = parse_worktree_list_porcelain(output)
    assert records == [
        {"worktree": "/a", "HEAD": "x", "branch": "refs/heads/main"}
    ]


# --- end-to-end against real git ----------------------------------------


def test_discover_happy_path(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["feature1", "feature2"])
    # One worktree using CDH's default template prefix; one not.
    make_worktree(repo_path, _isolate["dev_root"] / "myapp_worktree_feature1", "feature1")
    make_worktree(repo_path, _isolate["dev_root"] / "custom-dir", "feature2")
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    imported = body["imported"]
    names = sorted(w["name"] for w in imported)
    # Template-matching basename → stripped: feature1
    # Non-matching basename → verbatim: custom-dir → custom_dir after norm
    assert names == ["custom_dir", "feature1"]
    # Main checkout reported as skipped
    main_skips = [s for s in body["skipped"] if s["reason"] == "main checkout"]
    assert len(main_skips) == 1
    assert main_skips[0]["path"] == str(repo_path)


def test_discover_ticket_extraction(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["alice/COR-77_login-flow-fix"])
    make_worktree(
        repo_path,
        _isolate["dev_root"] / "myapp_worktree_COR-77_login_flow_fix",
        "alice/COR-77_login-flow-fix",
    )
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "branch_prefix": "alice/",
                "setup_steps": [],
                "ticket_pattern": r"COR-\d+",
            }
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/sync")
    body = r.json()
    assert len(body["imported"]) == 1
    entry = body["imported"][0]
    assert entry["ticket"] == "COR-77"
    assert entry["branch"] == "alice/COR-77_login-flow-fix"


def test_discover_skips_already_tracked(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["feature1"])
    wt_path = _isolate["dev_root"] / "myapp_worktree_feature1"
    make_worktree(repo_path, wt_path, "feature1")
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r1 = client.post("/api/worktrees/sync")
        assert r1.status_code == 200
        assert len(r1.json()["imported"]) == 1
        # Second run: same worktree on disk, already in DB.
        r2 = client.post("/api/worktrees/sync")
    assert r2.status_code == 200
    body = r2.json()
    assert body["imported"] == []
    already = [s for s in body["skipped"] if s["reason"] == "already tracked"]
    assert len(already) == 1
    assert already[0]["path"] == str(wt_path)


def test_discover_skips_detached_head(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["feature1"])
    wt_path = _isolate["dev_root"] / "detached-wt"
    # `git worktree add --detach <path> HEAD` produces a detached worktree
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(wt_path)],
        check=True,
        capture_output=True,
    )
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/sync")
    body = r.json()
    assert body["imported"] == []
    detached = [s for s in body["skipped"] if s["reason"] == "detached HEAD"]
    assert len(detached) == 1
    assert detached[0]["path"] == str(wt_path)


def test_discover_reports_missing_repo_path(_isolate: dict[str, Path]) -> None:
    bogus_path = _isolate["dev_root"] / "no-such-repo"
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "ghost",
                "path": str(bogus_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )
    with TestClient(app) as client:
        r = client.post("/api/worktrees/sync")
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == []
    missing = [s for s in body["skipped"] if s["reason"] == "repo path missing"]
    assert len(missing) == 1
    assert missing[0]["repo"] == "ghost"


def test_discover_isolates_per_repo_failures(_isolate: dict[str, Path]) -> None:
    """One broken repo (missing path) shouldn't block import for the
    other repos in the same call."""
    good_path = _isolate["dev_root"] / "good"
    init_git_repo(good_path, branches=["feature1"])
    make_worktree(good_path, _isolate["dev_root"] / "good_worktree_feature1", "feature1")

    bogus_path = _isolate["dev_root"] / "nope"
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "ghost",
                "path": str(bogus_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            },
            {
                "name": "good",
                "path": str(good_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            },
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/sync")
    body = r.json()
    # Good repo's worktree imported despite the ghost repo failing
    assert any(w["repo"] == "good" and w["name"] == "feature1" for w in body["imported"])
    assert any(s["repo"] == "ghost" and s["reason"] == "repo path missing" for s in body["skipped"])


def test_sync_noop_when_no_repos(_isolate: dict[str, Path]) -> None:
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[],
        iterm2=True,
    )
    with TestClient(app) as client:
        r = client.post("/api/worktrees/sync")
    assert r.status_code == 200
    assert r.json() == {"imported": [], "removed": [], "skipped": []}


def test_sync_removes_worktree_gone_from_git(_isolate: dict[str, Path]) -> None:
    """A worktree imported in one sync, then deleted via
    ``git worktree remove`` outside CDH, disappears on the next sync."""
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["feature1"])
    wt_path = _isolate["dev_root"] / "myapp_worktree_feature1"
    make_worktree(repo_path, wt_path, "feature1")
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r1 = client.post("/api/worktrees/sync")
        assert r1.status_code == 200
        assert len(r1.json()["imported"]) == 1
        assert r1.json()["removed"] == []

        # Remove the worktree the way a user would. --force isn't needed
        # since there are no uncommitted changes in the fixture.
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", str(wt_path)],
            check=True,
            capture_output=True,
        )

        r2 = client.post("/api/worktrees/sync")
    assert r2.status_code == 200
    body = r2.json()
    assert body["imported"] == []
    assert len(body["removed"]) == 1
    assert body["removed"][0]["name"] == "feature1"
    assert body["removed"][0]["path"] == str(wt_path)

    # Tracked row is actually gone from the DB.
    with TestClient(app) as client:
        r3 = client.get("/api/worktrees")
    assert r3.json()["worktrees"] == []


def test_sync_populates_pr_fields_on_import(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh pr view`` runs after each insert so freshly-imported rows
    have ``pr_number`` / ``pr_repo`` populated immediately. Without
    this, the inbox dedup join (which requires both fields non-null)
    misses the row and the PR shows up twice — once in inbox, once on
    the workspace's ``no PR yet`` tier — until the pr_state poll runs.
    """
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["feature"])
    make_worktree(
        repo_path,
        _isolate["dev_root"] / "myapp_worktree_feature",
        "feature",
    )
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )

    monkeypatch.setattr(
        worktree_import,
        "_gh_pr_view_sync",
        lambda wt_path: (60476, "headway/headway"),
    )

    with TestClient(app) as client:
        sync_resp = client.post("/api/worktrees/sync")
        assert sync_resp.status_code == 200, sync_resp.text
        rows = client.get("/api/worktrees").json()["worktrees"]

    imported = [r for r in rows if r["name"] == "feature"]
    assert len(imported) == 1
    assert imported[0]["pr_number"] == 60476
    assert imported[0]["pr_repo"] == "headway/headway"


def test_sync_handles_no_pr_gracefully(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_gh_pr_view_sync`` returning None (no PR yet for the branch,
    or gh missing/unauthed) must not block the import — the row gets
    inserted with null PR fields, same as the pre-fix behavior."""
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["feature"])
    make_worktree(
        repo_path,
        _isolate["dev_root"] / "myapp_worktree_feature",
        "feature",
    )
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
        iterm2=True,
    )

    monkeypatch.setattr(
        worktree_import, "_gh_pr_view_sync", lambda wt_path: None
    )

    with TestClient(app) as client:
        sync_resp = client.post("/api/worktrees/sync")
        assert sync_resp.status_code == 200, sync_resp.text
        rows = client.get("/api/worktrees").json()["worktrees"]

    imported = [r for r in rows if r["name"] == "feature"]
    assert len(imported) == 1
    assert imported[0]["pr_number"] is None
    assert imported[0]["pr_repo"] is None


def test_sync_does_not_remove_worktrees_in_other_repos(
    _isolate: dict[str, Path],
) -> None:
    """The removal pass is scoped to the repo being synced — a tracked
    row whose path is missing from repo A's worktree list shouldn't be
    dropped just because we synced repo B. Concretely: each repo's
    removal pass looks only at its own ``worktree`` rows.
    """
    repo_a = _isolate["dev_root"] / "a"
    repo_b = _isolate["dev_root"] / "b"
    init_git_repo(repo_a, branches=["feat"])
    init_git_repo(repo_b, branches=["feat"])
    wt_a = _isolate["dev_root"] / "a_worktree_feat"
    wt_b = _isolate["dev_root"] / "b_worktree_feat"
    make_worktree(repo_a, wt_a, "feat")
    make_worktree(repo_b, wt_b, "feat")
    write_minimal_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repos=[
            {
                "name": "a",
                "path": str(repo_a),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            },
            {
                "name": "b",
                "path": str(repo_b),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            },
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r1 = client.post("/api/worktrees/sync")
        assert len(r1.json()["imported"]) == 2
        # Remove only B's worktree
        subprocess.run(
            ["git", "-C", str(repo_b), "worktree", "remove", str(wt_b)],
            check=True,
            capture_output=True,
        )
        r2 = client.post("/api/worktrees/sync")
    body = r2.json()
    assert len(body["removed"]) == 1
    assert body["removed"][0]["repo"] == "b"
    # A's worktree is still tracked
    with TestClient(app) as client:
        rows = client.get("/api/worktrees").json()["worktrees"]
    assert {(r["repo"], r["name"]) for r in rows} == {("a", "feat")}
