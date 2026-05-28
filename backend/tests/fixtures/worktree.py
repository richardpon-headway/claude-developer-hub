"""Worktree row seeders + git-repo helpers for tests.

The seeders write directly to SQLite. Tests that need to exercise the
real ``create_worktree`` flow should instead use ``init_git_repo`` +
``write_repo_config`` and call the service.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path


def seed_worktree(
    db_path: Path,
    repo: str,
    name: str,
    *,
    path: Path | None = None,
    branch: str = "main",
    status: str = "ready",
    ticket: str | None = None,
    pr_number: int | None = None,
    pr_repo: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    mkdir: bool = True,
) -> None:
    """Insert one worktree row directly.

    If ``path`` is a Path, the directory is created on disk (matches the
    common "real path exists" test setup). Pass ``mkdir=False`` to leave
    the path uncreated — useful for tests of "path missing on disk"
    handling. If ``path`` is None, a stored-only string path of
    ``/tmp/<repo>_<name>`` is used and no directory is created — for
    pure-DB unit tests that only care about the row.
    """
    if path is None:
        stored_path = f"/tmp/{repo}_{name}"
    else:
        if mkdir:
            path.mkdir(parents=True, exist_ok=True)
        stored_path = str(path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # The worktree's (pr_repo, pr_number) FK references pr. When a
        # test seeds a worktree with a PR attached, make sure a parent
        # pr row exists so the INSERT doesn't trip the FK.
        if pr_repo is not None and pr_number is not None:
            conn.execute(
                "INSERT OR IGNORE INTO pr (pr_repo, pr_number) VALUES (?, ?)",
                (pr_repo, pr_number),
            )
        conn.execute(
            "INSERT INTO worktree "
            "(repo, name, path, branch, ticket, created_at, status, "
            " pr_number, pr_repo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo,
                name,
                stored_path,
                branch,
                ticket,
                created_at,
                status,
                pr_number,
                pr_repo,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def init_git_repo(path: Path, branches: list[str] | None = None) -> None:
    """Init a fresh git repo at ``path`` with ``main`` checked out + an
    initial commit. Any names in ``branches`` are created (not checked
    out) so they're available for ``git worktree add``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    for branch in branches or []:
        subprocess.run(["git", "-C", str(path), "branch", branch], check=True)


def make_worktree(repo_path: Path, target: Path, branch: str) -> None:
    """Shell out to ``git worktree add`` — for tests that exercise the
    real-git path (most don't; they seed a row via ``seed_worktree``)."""
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", str(target), branch],
        check=True,
        capture_output=True,
    )
