"""Tests for the Slice N worktree-import feature."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services.worktree_import import (
    parse_worktree_list_porcelain,
)

# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    dev_root = tmp_path / "dev"
    dev_root.mkdir()
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    return {"db_path": db_path, "config_path": config_path, "dev_root": dev_root}


def _write_config(
    config_path: Path,
    dev_root: Path,
    repos: list[dict],
) -> None:
    config_path.write_text(
        yaml.safe_dump(
            {
                "development_root": str(dev_root),
                "repos": repos,
                "iterm2": {"default_window": {"width": 800, "height": 600, "x": 0, "y": 0}},
            }
        )
    )


def _init_git_repo(path: Path, branches: list[str] | None = None) -> None:
    """Init a repo with `main` checked out + extra branches (not
    checked out) so we can `git worktree add` against them."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    for branch in branches or []:
        subprocess.run(["git", "-C", str(path), "branch", branch], check=True)


def _make_worktree(repo_path: Path, target: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", str(target), branch],
        check=True,
        capture_output=True,
    )


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
    _init_git_repo(repo_path, branches=["feature1", "feature2"])
    # One worktree using CDH's default template prefix; one not.
    _make_worktree(repo_path, _isolate["dev_root"] / "myapp_worktree_feature1", "feature1")
    _make_worktree(repo_path, _isolate["dev_root"] / "custom-dir", "feature2")
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        [
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/discover")
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
    _init_git_repo(repo_path, branches=["alice/COR-77_login-flow-fix"])
    _make_worktree(
        repo_path,
        _isolate["dev_root"] / "myapp_worktree_COR-77_login_flow_fix",
        "alice/COR-77_login-flow-fix",
    )
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        [
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "branch_prefix": "alice/",
                "setup_steps": [],
                "ticket_pattern": r"COR-\d+",
            }
        ],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/discover")
    body = r.json()
    assert len(body["imported"]) == 1
    entry = body["imported"][0]
    assert entry["ticket"] == "COR-77"
    assert entry["branch"] == "alice/COR-77_login-flow-fix"


def test_discover_skips_already_tracked(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path, branches=["feature1"])
    wt_path = _isolate["dev_root"] / "myapp_worktree_feature1"
    _make_worktree(repo_path, wt_path, "feature1")
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        [
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
    )

    with TestClient(app) as client:
        r1 = client.post("/api/worktrees/discover")
        assert r1.status_code == 200
        assert len(r1.json()["imported"]) == 1
        # Second run: same worktree on disk, already in DB.
        r2 = client.post("/api/worktrees/discover")
    assert r2.status_code == 200
    body = r2.json()
    assert body["imported"] == []
    already = [s for s in body["skipped"] if s["reason"] == "already tracked"]
    assert len(already) == 1
    assert already[0]["path"] == str(wt_path)


def test_discover_skips_detached_head(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path, branches=["feature1"])
    wt_path = _isolate["dev_root"] / "detached-wt"
    # `git worktree add --detach <path> HEAD` produces a detached worktree
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", "--detach", str(wt_path)],
        check=True,
        capture_output=True,
    )
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        [
            {
                "name": "myapp",
                "path": str(repo_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/discover")
    body = r.json()
    assert body["imported"] == []
    detached = [s for s in body["skipped"] if s["reason"] == "detached HEAD"]
    assert len(detached) == 1
    assert detached[0]["path"] == str(wt_path)


def test_discover_reports_missing_repo_path(_isolate: dict[str, Path]) -> None:
    bogus_path = _isolate["dev_root"] / "no-such-repo"
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        [
            {
                "name": "ghost",
                "path": str(bogus_path),
                "default_branch": "main",
                "setup_steps": [],
                "ticket_pattern": None,
            }
        ],
    )
    with TestClient(app) as client:
        r = client.post("/api/worktrees/discover")
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
    _init_git_repo(good_path, branches=["feature1"])
    _make_worktree(good_path, _isolate["dev_root"] / "good_worktree_feature1", "feature1")

    bogus_path = _isolate["dev_root"] / "nope"
    _write_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        [
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
    )

    with TestClient(app) as client:
        r = client.post("/api/worktrees/discover")
    body = r.json()
    # Good repo's worktree imported despite the ghost repo failing
    assert any(w["repo"] == "good" and w["name"] == "feature1" for w in body["imported"])
    assert any(s["repo"] == "ghost" and s["reason"] == "repo path missing" for s in body["skipped"])


def test_discover_noop_when_no_repos(_isolate: dict[str, Path]) -> None:
    _write_config(_isolate["config_path"], _isolate["dev_root"], [])
    with TestClient(app) as client:
        r = client.post("/api/worktrees/discover")
    assert r.status_code == 200
    assert r.json() == {"imported": [], "skipped": []}
