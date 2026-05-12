"""Worktree orchestration: SQLite persistence + git/setup_steps execution.

Responsibilities of this module:

- Read/write the ``worktree`` table.
- Run ``create_worktree`` as a background asyncio task: ``git fetch``,
  ``git worktree add``, then each ``setup_steps[]`` entry. Captures stdout
  (with stderr merged) into a bounded in-memory log buffer keyed by
  (repo, name). Updates the row's ``status`` as it progresses.

Subprocess work always goes through ``asyncio.create_subprocess_exec`` —
sync ``subprocess.run`` is banned in request handlers (would block the
event loop, freezing SSE for everyone).
"""
from __future__ import annotations

import asyncio
import collections
from pathlib import Path

from app.config.loader import load_config
from app.config.schema import RepoConfig
from app.db import get_db_path, open_db
from app.models.worktree import (
    WorktreeRow,
    derive_worktree_name,
    extract_ticket,
    now_iso,
)

LOG_BUFFER_MAX_LINES = 1000

_logs: dict[tuple[str, str], collections.deque[str]] = {}
_logs_lock = asyncio.Lock()


# --- log buffer -----------------------------------------------------------


async def _append_log(repo: str, name: str, line: str) -> None:
    async with _logs_lock:
        buf = _logs.get((repo, name))
        if buf is None:
            buf = collections.deque(maxlen=LOG_BUFFER_MAX_LINES)
            _logs[(repo, name)] = buf
        buf.append(line)


def get_log(repo: str, name: str) -> list[str]:
    buf = _logs.get((repo, name))
    return list(buf) if buf else []


def reset_log(repo: str, name: str) -> None:
    """Used by tests; not called from production code."""
    _logs.pop((repo, name), None)


# --- DB access (sync; wrap callers with asyncio.to_thread) ----------------


_LIST_SELECT = (
    "SELECT w.repo, w.name, w.path, w.branch, w.ticket, w.pr_number, w.pr_repo, "
    "       w.created_at, w.status, "
    "       (SELECT 1 FROM iterm_session s "
    "        WHERE s.repo = w.repo AND s.worktree_name = w.name "
    "          AND s.role = 'claude' LIMIT 1) IS NOT NULL "
    "FROM worktree w"
)


def _row_to_model(row: tuple) -> WorktreeRow:
    return WorktreeRow(
        repo=row[0],
        name=row[1],
        path=row[2],
        branch=row[3],
        ticket=row[4],
        pr_number=row[5],
        pr_repo=row[6],
        created_at=row[7],
        status=row[8],
        has_claude_session=bool(row[9]),
    )


def list_worktrees_sync(db_path: Path | None = None) -> list[WorktreeRow]:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(f"{_LIST_SELECT} ORDER BY w.repo, w.name")
        return [_row_to_model(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_worktree_sync(
    repo: str, name: str, db_path: Path | None = None
) -> WorktreeRow | None:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            f"{_LIST_SELECT} WHERE w.repo = ? AND w.name = ?",
            (repo, name),
        )
        row = cur.fetchone()
        return _row_to_model(row) if row else None
    finally:
        conn.close()


def insert_worktree_sync(row: WorktreeRow, db_path: Path | None = None) -> None:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO worktree (repo, name, path, branch, ticket, pr_number, "
            "pr_repo, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.repo,
                row.name,
                row.path,
                row.branch,
                row.ticket,
                row.pr_number,
                row.pr_repo,
                row.created_at,
                row.status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_worktree_status_sync(
    repo: str, name: str, status: str, db_path: Path | None = None
) -> None:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE worktree SET status = ? WHERE repo = ? AND name = ?",
            (status, repo, name),
        )
        conn.commit()
    finally:
        conn.close()


def update_worktree_pr_sync(
    repo: str,
    name: str,
    pr_number: int,
    pr_repo: str,
    db_path: Path | None = None,
) -> None:
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE worktree SET pr_number = ?, pr_repo = ? "
            "WHERE repo = ? AND name = ?",
            (pr_number, pr_repo, repo, name),
        )
        conn.commit()
    finally:
        conn.close()


# --- subprocess helper ----------------------------------------------------


async def _run_logged(
    repo: str,
    name: str,
    label: str,
    cmd: list[str] | str,
    cwd: Path,
    shell: bool = False,
) -> int:
    """Run a subprocess, merge stderr into stdout, stream each line into
    the in-memory log buffer. Returns the exit code. Never raises on
    non-zero exit — the caller decides whether non-zero is fatal.
    """
    await _append_log(repo, name, f"$ ({label}) {cmd if isinstance(cmd, str) else ' '.join(cmd)}")

    if shell:
        assert isinstance(cmd, str)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
    else:
        assert isinstance(cmd, list)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )

    assert proc.stdout is not None
    async for raw in proc.stdout:
        await _append_log(repo, name, raw.decode("utf-8", errors="replace").rstrip("\n"))
    return await proc.wait()


# --- orchestrator ---------------------------------------------------------


class WorktreeCreationError(Exception):
    """Caller-facing error during pre-flight validation. Raised synchronously
    by ``start_create_worktree`` before any background work is spawned."""


def _resolve_repo(repo_name: str) -> RepoConfig:
    config = load_config()
    for r in config.repos:
        if r.name == repo_name:
            return r
    raise WorktreeCreationError(f"unknown repo: {repo_name}")


def _resolve_target_path(repo: RepoConfig, short_name: str) -> Path:
    development_root = str(load_config().development_root)
    target = repo.worktree_path_template.format(
        development_root=development_root,
        repo=repo.name,
        short=short_name,
    )
    return Path(target)


async def _create_worktree_async(
    repo: RepoConfig,
    branch: str,
    short_name: str,
    target: Path,
    db_path: Path | None = None,
) -> None:
    """Run the actual git + setup_steps work. Updates DB status as it goes."""
    repo_path = Path(repo.path)

    # Step 1: fetch (best-effort — works offline or with no origin remote).
    # A non-zero exit code is logged but doesn't fail the creation; the
    # branch-verification step below catches actually-missing branches.
    rc = await _run_logged(
        repo.name, short_name, "fetch",
        ["git", "fetch", "origin", "--prune"],
        cwd=repo_path,
    )
    if rc != 0:
        await _append_log(
            repo.name, short_name,
            f"git fetch failed (exit {rc}); continuing with local branches only",
        )

    # Step 2: verify branch (local OR origin/<branch>); we don't care which
    rc_local = await _run_logged(
        repo.name, short_name, "verify-local",
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_path,
    )
    if rc_local != 0:
        rc_remote = await _run_logged(
            repo.name, short_name, "verify-remote",
            ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=repo_path,
        )
        if rc_remote != 0:
            await _append_log(
                repo.name, short_name,
                f"branch '{branch}' not found locally or on origin",
            )
            await asyncio.to_thread(
                update_worktree_status_sync, repo.name, short_name, "failed", db_path
            )
            return

    # Step 3: worktree add
    rc = await _run_logged(
        repo.name, short_name, "worktree-add",
        ["git", "worktree", "add", str(target), branch],
        cwd=repo_path,
    )
    if rc != 0:
        await asyncio.to_thread(
            update_worktree_status_sync, repo.name, short_name, "failed", db_path
        )
        return

    # Step 4: setup_steps (config-driven; no hardcoded mise/make/pnpm)
    for i, step in enumerate(repo.setup_steps):
        step_cwd = target / step.cwd if step.cwd else target
        rc = await _run_logged(
            repo.name, short_name, f"setup-step-{i}",
            step.cmd,
            cwd=step_cwd,
            shell=True,
        )
        if rc != 0:
            await _append_log(
                repo.name, short_name,
                f"setup step {i} failed (exit {rc})",
            )
            await asyncio.to_thread(
                update_worktree_status_sync, repo.name, short_name, "failed", db_path
            )
            return

    # All good
    await asyncio.to_thread(
        update_worktree_status_sync, repo.name, short_name, "ready", db_path
    )


async def create_worktree(
    repo_name: str, branch: str, db_path: Path | None = None
) -> WorktreeRow:
    """Create a worktree end-to-end: validate, insert row, run git +
    setup_steps, return the final row.

    This blocks the request for the duration of setup (potentially tens of
    seconds for large repos). For Slice E that's the simplest reliable
    design — TestClient and asyncio.create_task interact badly enough that
    background-task scheduling isn't worth the debugging cost yet. A
    future slice will introduce a proper worker (lifespan-owned task
    + asyncio.Queue) so the POST can return immediately.

    Raises ``WorktreeCreationError`` for client-correctable preconditions
    (unknown repo, name collision, target already exists) BEFORE inserting
    a row, so callers get a clean 4xx and no orphan rows.
    """
    repo = _resolve_repo(repo_name)
    short_name = derive_worktree_name(branch, repo.branch_prefix, repo.ticket_pattern)

    existing = await asyncio.to_thread(get_worktree_sync, repo_name, short_name, db_path)
    if existing is not None:
        raise WorktreeCreationError(
            f"a worktree already exists for repo={repo_name} "
            f"name={short_name} (status={existing.status})"
        )

    target = _resolve_target_path(repo, short_name)
    if target.exists():
        raise WorktreeCreationError(f"target path already exists on disk: {target}")

    row = WorktreeRow(
        repo=repo.name,
        name=short_name,
        path=str(target),
        branch=branch,
        ticket=extract_ticket(branch, repo.ticket_pattern),
        pr_number=None,
        pr_repo=None,
        created_at=now_iso(),
        status="setting_up",
    )
    await asyncio.to_thread(insert_worktree_sync, row, db_path)

    await _create_worktree_async(repo, branch, short_name, target, db_path)

    final = await asyncio.to_thread(get_worktree_sync, repo.name, short_name, db_path)
    assert final is not None  # we just inserted it
    return final
