"""Tests for GET /api/repos/candidates — auto-discovery from development_root."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.fixtures.config import write_minimal_config


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


def _init_git_repo(path: Path, branches: list[str] | None = None) -> None:
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


def _config_repo_entry(name: str, path: Path) -> dict:
    return {
        "name": name,
        "path": str(path),
        "default_branch": "main",
        "setup_steps": [],
        "ticket_pattern": None,
    }


def test_lists_git_repos_excludes_plain_dirs(_isolate: dict[str, Path]) -> None:
    dev = _isolate["dev_root"]
    _init_git_repo(dev / "alpha")
    _init_git_repo(dev / "beta")
    (dev / "not-a-repo").mkdir()
    write_minimal_config(_isolate["config_path"], dev, iterm2=True)

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    assert r.status_code == 200
    names = [c["name"] for c in r.json()]
    assert "alpha" in names and "beta" in names
    assert "not-a-repo" not in names


def test_excludes_standard_worktrees(_isolate: dict[str, Path]) -> None:
    """A `git worktree add`-ed worktree has `.git` as a regular file
    (gitdir: pointer). It should not appear as a candidate."""
    dev = _isolate["dev_root"]
    parent = dev / "myapp"
    _init_git_repo(parent, branches=["feature"])
    worktree = dev / "myapp_worktree_feature"
    subprocess.run(
        ["git", "-C", str(parent), "worktree", "add", str(worktree), "feature"],
        check=True,
        capture_output=True,
    )
    write_minimal_config(_isolate["config_path"], dev, iterm2=True)

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    names = [c["name"] for c in r.json()]
    assert "myapp" in names
    assert "myapp_worktree_feature" not in names


def test_excludes_symlinked_worktree(_isolate: dict[str, Path]) -> None:
    """Rare manual setup: `.git` is a symlink resolving into another
    repo's `.git/worktrees/<n>`. Should be excluded."""
    dev = _isolate["dev_root"]
    parent = dev / "myapp"
    _init_git_repo(parent, branches=["feature"])
    real_worktree = dev / "real_worktree"
    subprocess.run(
        ["git", "-C", str(parent), "worktree", "add", str(real_worktree), "feature"],
        check=True,
        capture_output=True,
    )
    # The real worktree's gitdir lives at parent/.git/worktrees/real_worktree
    worktree_gitdir = parent / ".git" / "worktrees" / "real_worktree"
    assert worktree_gitdir.is_dir()

    # Construct a sibling dir whose `.git` symlinks to that gitdir.
    fake_main = dev / "looks-like-a-repo"
    fake_main.mkdir()
    (fake_main / ".git").symlink_to(worktree_gitdir)
    write_minimal_config(_isolate["config_path"], dev, iterm2=True)

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    names = [c["name"] for c in r.json()]
    assert "myapp" in names
    assert "real_worktree" not in names
    assert "looks-like-a-repo" not in names  # excluded via .git/worktrees/ check


def test_includes_symlinked_main_checkout(_isolate: dict[str, Path]) -> None:
    """A `.git` symlink resolving to a real main-checkout `.git`
    directory (NOT inside `.git/worktrees/`) should be included.
    This matches the iCloud/Dropbox-symlinked-repo case."""
    dev = _isolate["dev_root"]
    real_repo = _isolate["dev_root"].parent / "external" / "my-repo"
    real_repo.mkdir(parents=True)
    _init_git_repo(real_repo)
    # Make a dir inside dev_root whose .git symlinks to the external repo's .git
    visible = dev / "visible-repo"
    visible.mkdir()
    (visible / ".git").symlink_to(real_repo / ".git")
    write_minimal_config(_isolate["config_path"], dev, iterm2=True)

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    names = [c["name"] for c in r.json()]
    assert "visible-repo" in names


def test_excludes_hidden_dirs(_isolate: dict[str, Path]) -> None:
    dev = _isolate["dev_root"]
    _init_git_repo(dev / "ok")
    # Hidden dir that happens to be a git repo (e.g., user dotdir) — skip
    _init_git_repo(dev / ".cache")
    write_minimal_config(_isolate["config_path"], dev, iterm2=True)

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    names = [c["name"] for c in r.json()]
    assert names == ["ok"]


def test_already_configured_flag(_isolate: dict[str, Path]) -> None:
    dev = _isolate["dev_root"]
    _init_git_repo(dev / "configured-one")
    _init_git_repo(dev / "fresh-one")
    write_minimal_config(
        _isolate["config_path"],
        dev,
        repos=[_config_repo_entry("configured-one", dev / "configured-one")],
        iterm2=True,
    )

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    by_name = {c["name"]: c for c in r.json()}
    assert by_name["configured-one"]["already_configured"] is True
    assert by_name["fresh-one"]["already_configured"] is False


def test_sort_order_not_configured_first_then_alpha(
    _isolate: dict[str, Path],
) -> None:
    dev = _isolate["dev_root"]
    _init_git_repo(dev / "zeta")
    _init_git_repo(dev / "alpha")
    _init_git_repo(dev / "configured-a")
    _init_git_repo(dev / "configured-z")
    write_minimal_config(
        _isolate["config_path"],
        dev,
        repos=[
            _config_repo_entry("ca", dev / "configured-a"),
            _config_repo_entry("cz", dev / "configured-z"),
        ],
        iterm2=True,
    )

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    names = [c["name"] for c in r.json()]
    # Not-configured first (alpha sort), then configured (alpha sort)
    assert names == ["alpha", "zeta", "configured-a", "configured-z"]


def test_empty_when_dev_root_missing(_isolate: dict[str, Path]) -> None:
    bogus = _isolate["dev_root"].parent / "no-such-dir"
    _isolate["config_path"].write_text(
        yaml.safe_dump(
            {
                "development_root": str(bogus),
                "repos": [],
                "iterm2": {"default_window": {"width": 800, "height": 600, "x": 0, "y": 0}},
            }
        )
    )

    with TestClient(app) as client:
        r = client.get("/api/repos/candidates")
    assert r.status_code == 200
    assert r.json() == []
