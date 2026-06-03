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

# Strong-ref dict of in-flight background setup tasks, keyed by
# (repo, name). Two responsibilities: keep the asyncio.Task alive (it
# would otherwise be GC'd because asyncio holds only a weak ref) and
# expose the lookup so a second concurrent ``create_worktree`` call
# for the same key can return the existing setting_up row instead of
# racing a duplicate ``git worktree add``. Cleared by the lifespan
# shutdown handler in app.main and by the per-test isolation fixture.
_setting_up_tasks: dict[tuple[str, str], asyncio.Task] = {}

# Serializes the check-existing → insert → spawn sequence in
# ``create_worktree`` so two concurrent callers can't both insert and
# trip the worktree (repo, name) UNIQUE constraint. The dict guard
# ``key in _setting_up_tasks`` only works once the first call has
# spawned its task — without this lock, two calls can both see
# ``existing=None`` and race the INSERT.
_create_lock = asyncio.Lock()


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
    bookmark/authored surfaces fill in the metadata fields later via
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


async def _run_setup_with_recovery(
    repo: RepoConfig,
    branch: str,
    short_name: str,
    target: Path,
    db_path: Path | None = None,
) -> None:
    """Wrap ``_create_worktree_async`` so an uncaught exception inside
    setup flips the row to a terminal status instead of leaking a
    stuck ``setting_up`` row for the lifetime of the process.

    Routes the recovery status using the same path-existence check the
    lifespan reconciler uses (``app.db._reconcile_orphaned_setting_up``):
    target exists on disk → ``code_on_disk``; missing → ``failed``.

    ``CancelledError`` is allowed to propagate so the lifespan shutdown
    handler can drain in-flight tasks cleanly; the next backend boot's
    reconciler picks up any rows left in ``setting_up`` by a cancel.
    """
    try:
        await _create_worktree_async(repo, branch, short_name, target, db_path)
    except asyncio.CancelledError:
        raise
    except Exception:
        recovery_status = "code_on_disk" if target.is_dir() else "failed"
        log.exception(
            "background setup raised; routing row to recovery status",
            extra={
                "repo_name": repo.name,
                "worktree_name": short_name,
                "recovery_status": recovery_status,
            },
        )
        await asyncio.to_thread(
            update_worktree_status_sync,
            repo.name, short_name, recovery_status, db_path,
        )


def _spawn_setup_task(
    repo: RepoConfig,
    branch: str,
    short_name: str,
    target: Path,
    db_path: Path | None = None,
) -> asyncio.Task:
    """Schedule the background setup task, register it in
    ``_setting_up_tasks`` so it survives GC, and arrange auto-removal
    on completion. Mirrors the strong-ref pattern in
    ``app.routes.worktrees._post_spawn_tasks``.
    """
    key = (repo.name, short_name)
    task = asyncio.create_task(
        _run_setup_with_recovery(repo, branch, short_name, target, db_path),
        name=f"create_worktree:{repo.name}/{short_name}",
    )
    _setting_up_tasks[key] = task
    task.add_done_callback(lambda _t: _setting_up_tasks.pop(key, None))
    return task


async def create_worktree(
    repo_name: str, branch: str, db_path: Path | None = None
) -> WorktreeRow:
    """Create a worktree end-to-end, idempotently. Returns AS SOON AS
    the row is inserted (status ``setting_up``); the rest of setup —
    ``git fetch``, ``git worktree add``, ``setup_steps`` — runs as a
    background task tracked in ``_setting_up_tasks``.

    Resolves the target row for ``(repo, name)``:

    - **No existing row** — insert one in ``setting_up`` and spawn the
      background setup task. Return the inserted row immediately.
    - **Existing row, same branch, status=ready** — return it as a
      no-op. The first attempt already completed.
    - **Existing row, same branch, in-flight task** — return the
      existing setting_up row without spawning a duplicate task.
      Guards against a concurrent double-click on the same
      ``(repo, name)`` from racing two ``git worktree add`` calls on
      the same path.
    - **Existing row, same branch, status in {setting_up, failed,
      code_on_disk} with no in-flight task** — treat as a resume
      point. Flip status to ``setting_up`` and spawn a new background
      task. Each git step detects what's already in place and skips
      or recovers (see ``_create_worktree_async``).
    - **Existing row, different branch** — raise
      ``WorktreeCreationError``. A genuinely different branch chose
      the same short name; the user picks a different name or
      deletes the existing.

    The returned row always reflects the row's state at the moment of
    spawn. Callers that need terminal status (tests, manual
    verification scripts) must await ``wait_for_setup_complete`` and
    re-fetch the row, or use the ``create_and_wait`` test helper.

    Raises ``WorktreeCreationError`` for the unknown-repo and
    branch-mismatch preconditions BEFORE any DB write, so callers get
    a clean 4xx and no orphan rows.
    """
    repo = _resolve_repo(repo_name)
    short_name = derive_worktree_name(branch, repo.branch_prefix, repo.ticket_pattern)
    target = _resolve_target_path(repo, short_name)
    key = (repo.name, short_name)

    async with _create_lock:
        return await _check_or_insert_then_spawn(
            repo, branch, short_name, target, key, db_path
        )


async def _check_or_insert_then_spawn(
    repo: RepoConfig,
    branch: str,
    short_name: str,
    target: Path,
    key: tuple[str, str],
    db_path: Path | None,
) -> WorktreeRow:
    """Inner body of ``create_worktree``, held under ``_create_lock``.

    Separated so the lock scope is obvious from the call site and the
    early-return short-circuits stay readable.
    """
    existing = await asyncio.to_thread(
        get_worktree_sync, repo.name, short_name, db_path
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
        if key in _setting_up_tasks:
            # Concurrent click on the same (repo, name) while a setup
            # task is already running. Return the existing setting_up
            # row without spawning a second task — plan-66's
            # idempotency covered sequential retries; this covers the
            # concurrent case.
            log.info(
                "setup already in flight; returning existing row",
                extra={"repo_name": repo.name, "worktree_name": short_name},
            )
            return existing
        log.info(
            "resuming existing worktree row mid-create",
            extra={
                "repo_name": repo.name,
                "worktree_name": short_name,
                "existing_status": existing.status,
            },
        )
        # Flip back to setting_up so concurrent readers see the resume
        # in flight rather than the stale terminal status.
        await asyncio.to_thread(
            update_worktree_status_sync, repo.name, short_name, "setting_up", db_path
        )
        row = existing.model_copy(update={"status": "setting_up"})
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

    _spawn_setup_task(repo, branch, short_name, target, db_path)
    return row


# --- test helpers ---------------------------------------------------------


async def wait_for_setup_complete(
    repo: str | None = None, name: str | None = None
) -> None:
    """Await in-flight background setup tasks.

    With both args ``None``, drains every task in ``_setting_up_tasks``.
    With ``(repo, name)`` set, awaits only that specific task if it's
    in flight. Used by tests to assert terminal status after calling
    ``create_worktree`` — production code doesn't need this because the
    Hub UI watches the row's status via the existing 5s poll.
    """
    if repo is not None and name is not None:
        task = _setting_up_tasks.get((repo, name))
        tasks: list[asyncio.Task] = [task] if task is not None else []
    else:
        tasks = list(_setting_up_tasks.values())
    if not tasks:
        return
    await asyncio.gather(*tasks, return_exceptions=True)


async def create_and_wait(
    repo_name: str, branch: str, db_path: Path | None = None
) -> WorktreeRow:
    """Spawn ``create_worktree`` and wait for setup to finish, then
    return the terminal-status row. Test-only convenience that mirrors
    the pre-plan-67 synchronous return contract of ``create_worktree``.
    """
    row = await create_worktree(repo_name, branch, db_path)
    await wait_for_setup_complete(row.repo, row.name)
    final = await asyncio.to_thread(
        get_worktree_sync, row.repo, row.name, db_path
    )
    if final is None:
        raise WorktreeCreationError(
            f"worktree row for {row.repo}/{row.name} disappeared during setup"
        )
    return final
