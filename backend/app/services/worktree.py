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
import logging
import os
import shutil
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
from app.services import git_cli

log = logging.getLogger(__name__)

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
    "       pr.author_login, w.notes, w.created_at, w.status, "
    "       (SELECT 1 FROM terminal_session s "
    "        WHERE s.repo = w.repo AND s.worktree_name = w.name "
    "          AND s.role = 'claude' LIMIT 1) IS NOT NULL, "
    "       ps.payload, ps.checked_at "
    "FROM worktree w "
    "LEFT JOIN pr "
    "  ON pr.pr_repo = w.pr_repo AND pr.pr_number = w.pr_number "
    "LEFT JOIN pr_state ps "
    "  ON ps.pr_repo = w.pr_repo AND ps.pr_number = w.pr_number"
)


def _row_to_model(row: tuple) -> WorktreeRow:
    import json

    from app.models.worktree import PrStateSummary

    pr_state: PrStateSummary | None = None
    payload_json = row[12]
    checked_at = row[13]
    if payload_json is not None and checked_at is not None:
        try:
            data = json.loads(payload_json)
            data["checked_at"] = checked_at
            # Back-compat: rows written before the multi-label change
            # have no ``labels`` in payload. Fall back to a one-element
            # list derived from the headline so the frontend renders.
            if "labels" not in data:
                data["labels"] = [data["headline"]] if data.get("headline") else []
            pr_state = PrStateSummary.model_validate(data)
        except Exception:
            # Bad JSON in the cache row shouldn't sink the list query —
            # surface as "no pr_state" and let the next poll repair.
            pr_state = None

    return WorktreeRow(
        repo=row[0],
        name=row[1],
        path=row[2],
        branch=row[3],
        ticket=row[4],
        pr_number=row[5],
        pr_repo=row[6],
        pr_author_login=row[7],
        notes=row[8],
        created_at=row[9],
        status=row[10],
        has_claude_session=bool(row[11]),
        pr_state=pr_state,
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


def list_worktree_paths_for_repo_sync(
    repo: str, db_path: Path | None = None
) -> list[tuple[str, str]]:
    """Return ``[(name, path), …]`` for every row this repo owns. Used by
    the sync flow to find tracked worktrees whose path is no longer in
    git's worktree list (and therefore should be removed from the DB)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT name, path FROM worktree WHERE repo = ?", (repo,)
        )
        return [(row[0], row[1]) for row in cur.fetchall()]
    finally:
        conn.close()


def delete_worktree_sync(
    repo: str, name: str, db_path: Path | None = None
) -> int:
    """Delete a worktree row. ``iterm_session`` and ``pr_state`` rows
    cascade away via FK ON DELETE CASCADE. Returns the row count actually
    deleted (0 if no matching row)."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM worktree WHERE repo = ? AND name = ?", (repo, name)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_worktree_pr_sync(
    repo: str,
    name: str,
    pr_number: int,
    pr_repo: str,
    db_path: Path | None = None,
) -> None:
    """Set the worktree's PR identifiers.

    Inserts an empty pr row first (if one doesn't already exist) so
    the worktree's FK to `pr` resolves. The pr_state poll and the
    bookmark/inbox surfaces fill in the metadata fields later via
    upsert; this just guarantees the linkage doesn't dangle.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO pr (pr_repo, pr_number) VALUES (?, ?)",
            (pr_repo, pr_number),
        )
        conn.execute(
            "UPDATE worktree SET pr_number = ?, pr_repo = ? "
            "WHERE repo = ? AND name = ?",
            (pr_number, pr_repo, repo, name),
        )
        conn.commit()
    finally:
        conn.close()


def update_worktree_notes_sync(
    repo: str,
    name: str,
    notes: str,
    db_path: Path | None = None,
) -> None:
    """Overwrite the free-form notes column on a worktree row.

    Empty string is a valid value — the UI uses it to clear a note —
    so we don't coerce empty → NULL. The distinction never matters
    to the read path (both render as "no notes")."""
    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE worktree SET notes = ? WHERE repo = ? AND name = ?",
            (notes, repo, name),
        )
        conn.commit()
    finally:
        conn.close()


# --- subprocess helper ----------------------------------------------------


def _subprocess_env() -> dict[str, str]:
    """Prepend mise's shims dir to PATH so mise-managed tools resolve in subprocesses."""
    env = os.environ.copy()
    mise_data = os.environ.get("MISE_DATA_DIR") or str(Path.home() / ".local/share/mise")
    shims = Path(mise_data) / "shims"
    if shims.is_dir():
        env["PATH"] = f"{shims}{os.pathsep}{env.get('PATH', '')}"
    return env


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
            env=_subprocess_env(),
        )
    else:
        assert isinstance(cmd, list)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=_subprocess_env(),
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
    """Run the actual git + setup_steps work. Updates DB status as it goes.

    Idempotent under retry: if a prior attempt was killed mid-flight,
    each git step detects the partially-completed state and either
    skips the step (worktree already registered to this branch) or
    cleans up the stray on-disk state (untracked directory at the
    target path) before continuing.
    """
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

    # Step 3: worktree add — pre-check for partial state from a prior
    # killed attempt.
    target_str = str(target)
    existing_wts = await git_cli.list_git_worktrees(repo_path)
    registered = next((w for w in existing_wts if w.path == target_str), None)
    if registered is not None:
        if registered.branch == branch:
            # Prior attempt already added the worktree to git's tracking
            # for the same branch — skip the add and continue into setup.
            await _append_log(
                repo.name, short_name,
                f"git worktree already registered at {target} for branch '{branch}'; resuming",
            )
        else:
            await _append_log(
                repo.name, short_name,
                f"target path {target} is registered as a worktree for a "
                f"different branch ({registered.branch!r}); refusing to overwrite",
            )
            await asyncio.to_thread(
                update_worktree_status_sync, repo.name, short_name, "failed", db_path
            )
            return
    else:
        if target.exists():
            # Untracked stray directory (left over from a prior killed
            # attempt before `git worktree add` registered it, or
            # placed by the user). Clear it + prune git's stale
            # tracking so the upcoming add can succeed.
            await _append_log(
                repo.name, short_name,
                f"removing stray directory at {target} before worktree add",
            )
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            prune = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_path), "worktree", "prune",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await prune.wait()
        rc = await _run_logged(
            repo.name, short_name, "worktree-add",
            ["git", "worktree", "add", target_str, branch],
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
            # `git worktree add` already succeeded above, so the code
            # IS on disk — only the bootstrap automation failed. Mark
            # as `code_on_disk` (not `failed`) so the user isn't
            # locked out of the action buttons; they can open the
            # worktree in iTerm2 / Cursor and re-run the failing
            # step manually.
            await asyncio.to_thread(
                update_worktree_status_sync,
                repo.name, short_name, "code_on_disk", db_path,
            )
            return

    # All good
    await asyncio.to_thread(
        update_worktree_status_sync, repo.name, short_name, "ready", db_path
    )


async def create_worktree(
    repo_name: str, branch: str, db_path: Path | None = None
) -> WorktreeRow:
    """Create a worktree end-to-end, idempotently.

    Validates the repo, then resolves the target row for ``(repo, name)``:

    - **No existing row** — insert one in ``setting_up`` and run the
      full git + setup_steps flow.
    - **Existing row, same branch, status=ready** — return it
      immediately as a no-op. The first attempt already completed.
    - **Existing row, same branch, status in {setting_up, failed,
      code_on_disk}** — treat as a resume point. Re-run the git +
      setup_steps flow; each step detects what's already in place
      and skips or recovers.
    - **Existing row, different branch** — raise
      ``WorktreeCreationError``. A genuinely different branch chose
      the same short name; the user picks a different name or
      deletes the existing.

    Blocks the request for the duration of setup (potentially tens of
    seconds for large repos). TestClient + asyncio.create_task interact
    badly enough that background-task scheduling isn't worth the
    debugging cost yet.

    Raises ``WorktreeCreationError`` for the unknown-repo and
    branch-mismatch preconditions BEFORE any DB write, so callers get a
    clean 4xx and no orphan rows.
    """
    repo = _resolve_repo(repo_name)
    short_name = derive_worktree_name(branch, repo.branch_prefix, repo.ticket_pattern)
    target = _resolve_target_path(repo, short_name)

    existing = await asyncio.to_thread(
        get_worktree_sync, repo_name, short_name, db_path
    )
    if existing is not None:
        if existing.branch != branch:
            raise WorktreeCreationError(
                f"worktree '{short_name}' already exists for a different "
                f"branch ({existing.branch!r}) — pick a different name or "
                f"delete the existing"
            )
        if existing.status == "ready":
            return existing
        log.info(
            "resuming existing worktree row mid-create",
            extra={
                "repo": repo.name,
                "name": short_name,
                "existing_status": existing.status,
            },
        )
        # Flip back to setting_up so concurrent readers see the resume
        # in flight rather than the stale terminal status.
        await asyncio.to_thread(
            update_worktree_status_sync, repo.name, short_name, "setting_up", db_path
        )
    else:
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

    final = await asyncio.to_thread(
        get_worktree_sync, repo.name, short_name, db_path
    )
    assert final is not None  # we just inserted or resumed it
    return final
