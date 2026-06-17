"""Sync CDH's worktree table with git's view of the world.

CDH normally tracks only worktrees it created itself (via the worktree
create endpoint). The sync flow reconciles per-configured-repo:

- New worktrees that git knows about but CDH doesn't → insert rows.
- Tracked rows whose path is no longer in git's worktree list (user
  ran ``git worktree remove`` or ``rm -rf`` outside CDH) → delete rows.

Rows in transient states (``setting_up``, ``removing``) are left alone
since they're owned by an in-flight task that hasn't published the
worktree to git yet (or is mid-tear-down).

The parser handles the ``git worktree list --porcelain`` format, which
emits a small record per worktree separated by blank lines. Each record
has at minimum a ``worktree <path>`` line; depending on the worktree's
state it may also have ``HEAD <sha>``, ``branch refs/heads/<name>``,
``bare``, ``detached``, ``locked``, or ``prunable`` lines.

Detached-HEAD worktrees are intentionally skipped. They exist mainly
for ``gh pr checkout`` flows; importing them would create workspace
rows with no branch, which most of CDH's downstream features
(ticket extraction, send-skill button labels) can't reason about. The
README documents this limitation.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from app.config.loader import load_config
from app.config.schema import RepoConfig
from app.db import get_db_path
from app.models.worktree import (
    WorktreeRow,
    derive_worktree_name,
    extract_ticket,
    now_iso,
)
from app.services.worktree import (
    delete_worktree_sync,
    get_worktree_sync,
    insert_worktree_sync,
    list_worktree_paths_for_repo_sync,
    update_worktree_pr_sync,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_worktree_list_porcelain(output: str) -> list[dict]:
    """Parse the multi-record output of ``git worktree list --porcelain``.

    Each record is a series of ``key value?`` lines followed by a blank
    line. The first line of each record is always ``worktree <path>``.
    Bare/detached/locked/prunable are presence flags (no value).
    """
    records: list[dict] = []
    current: dict | None = None
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            if current is not None:
                records.append(current)
                current = None
            continue
        if current is None:
            current = {}
        key, _, value = line.partition(" ")
        if value == "":
            current[key] = True
        else:
            current[key] = value
    if current is not None:
        records.append(current)
    return records


# ---------------------------------------------------------------------------
# Skip rules + name derivation
# ---------------------------------------------------------------------------


def _branch_from_record(record: dict) -> str | None:
    """``branch`` is ``refs/heads/<name>`` when present; absent for detached."""
    raw = record.get("branch")
    if isinstance(raw, str):
        return re.sub(r"^refs/heads/", "", raw)
    return None


def _derive_imported_name(
    repo: RepoConfig, on_disk_path: Path, branch: str
) -> str:
    """Derive the workspace name for an existing worktree.

    Step 1: if the basename starts with ``<repo>_worktree_``, strip
    that prefix and use the remainder (this matches CDH's default
    ``worktree_path_template``).

    Step 2: otherwise, fall back to the basename verbatim.

    Step 3: apply the same hyphen-to-underscore normalization as
    create_worktree, preserving ticket-pattern matches.
    """
    basename = on_disk_path.name
    expected_prefix = f"{repo.name}_worktree_"
    if basename.startswith(expected_prefix):
        short = basename[len(expected_prefix):]
    else:
        short = basename
    # Use the same normalization as the create path so an imported
    # `feature-x` and a CDH-created `feature-x` would collide on name
    # rather than producing two near-identical entries.
    return derive_worktree_name(short, "", repo.ticket_pattern)


# ---------------------------------------------------------------------------
# Per-repo discovery
# ---------------------------------------------------------------------------


def _run_git_worktree_list(repo_path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git worktree list failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip() or '(no output)'}"
        )
    return proc.stdout


def sync_worktrees_for_repo_sync(
    repo: RepoConfig, db_path: Path | None = None
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return ``(imported, removed, skipped, relinked)`` for one repo.

    Each ``imported`` entry includes ``repo``/``name``/``path``/
    ``branch``/``ticket``. Each ``removed`` entry includes ``repo``/
    ``name``/``path``/``reason``. Each ``skipped`` entry includes
    ``repo``/``path``/``reason``. Each ``relinked`` entry includes
    ``repo``/``name``/``path``/``pr_repo``/``pr_number`` — an
    already-tracked worktree whose PR was opened after first import and
    has now been backfilled (so it dedupes against its PR card).

    Removal: any tracked row whose path is no longer in git's
    ``worktree list`` output is dropped (transient rows in
    ``setting_up``/``removing`` are spared — those are owned by an
    in-flight task).
    """
    imported: list[dict] = []
    removed: list[dict] = []
    skipped: list[dict] = []
    relinked: list[dict] = []

    repo_path = Path(repo.path)
    if not repo_path.is_dir():
        skipped.append(
            {"repo": repo.name, "path": str(repo_path), "reason": "repo path missing"}
        )
        return imported, removed, skipped, relinked

    try:
        output = _run_git_worktree_list(repo_path)
    except RuntimeError as e:
        log.warning("git worktree list failed for %s: %s", repo.name, e)
        skipped.append(
            {"repo": repo.name, "path": str(repo_path), "reason": f"git failed: {e}"}
        )
        return imported, removed, skipped, relinked

    records = parse_worktree_list_porcelain(output)
    git_paths: set[str] = set()
    for rec in records:
        wt_path_str = rec.get("worktree")
        if isinstance(wt_path_str, str):
            git_paths.add(wt_path_str)

    for rec in records:
        wt_path_str = rec.get("worktree")
        if not isinstance(wt_path_str, str):
            # Defensive: empty record. Should never happen.
            continue
        wt_path = Path(wt_path_str)

        if rec.get("bare"):
            skipped.append({"repo": repo.name, "path": str(wt_path), "reason": "bare"})
            continue
        if rec.get("prunable"):
            skipped.append(
                {"repo": repo.name, "path": str(wt_path), "reason": "prunable"}
            )
            continue
        branch = _branch_from_record(rec)
        if branch is None:
            # Detached HEAD — no branch line in porcelain output.
            skipped.append(
                {"repo": repo.name, "path": str(wt_path), "reason": "detached HEAD"}
            )
            continue
        if wt_path == repo_path:
            skipped.append(
                {"repo": repo.name, "path": str(wt_path), "reason": "main checkout"}
            )
            continue

        # Already tracked? (path-based check; the (repo, name) collision
        # check happens below after name derivation.)
        existing_at_path = _get_worktree_by_path_sync(
            repo.name, str(wt_path), db_path
        )
        if existing_at_path is not None:
            # A worktree whose branch had no PR at first import (the common
            # branch-first → open-PR-later flow) stays at pr_number IS NULL
            # forever otherwise: the one-shot link below only runs on the
            # freshly-inserted row, and the enrichment poller refreshes PR
            # metadata but never *creates* the worktree→pr link. So an
            # opened-after-import PR would surface as its own authored/
            # bookmarked card alongside this "no PR yet" worktree — the
            # same work shown twice. Re-check here so Sync reconciles it.
            if existing_at_path.pr_number is None:
                pr_info = _gh_pr_view_sync(wt_path)
                if pr_info is not None:
                    update_worktree_pr_sync(
                        repo.name,
                        existing_at_path.name,
                        pr_info[0],
                        pr_info[1],
                        db_path=db_path,
                    )
                    relinked.append(
                        {
                            "repo": repo.name,
                            "name": existing_at_path.name,
                            "path": str(wt_path),
                            "pr_repo": pr_info[1],
                            "pr_number": pr_info[0],
                        }
                    )
                    continue
            skipped.append(
                {"repo": repo.name, "path": str(wt_path), "reason": "already tracked"}
            )
            continue

        name = _derive_imported_name(repo, wt_path, branch)
        existing_by_name = get_worktree_sync(repo.name, name, db_path)
        if existing_by_name is not None:
            skipped.append(
                {
                    "repo": repo.name,
                    "path": str(wt_path),
                    "reason": "name collision",
                }
            )
            continue

        row = WorktreeRow(
            repo=repo.name,
            name=name,
            path=str(wt_path),
            branch=branch,
            ticket=extract_ticket(branch, repo.ticket_pattern),
            pr_number=None,
            pr_repo=None,
            created_at=now_iso(),
            status="ready",
        )
        insert_worktree_sync(row, db_path)
        # Immediately populate PR fields so the workspace lands in the
        # right tier and the worktree-dedup join (which gates on
        # ``pr_number IS NOT NULL``) catches the row right away. Failures
        # (no PR yet, gh missing, auth/network) are silent — the
        # already-tracked re-link pass above retries on a later sync once
        # a PR exists for the branch.
        pr_info = _gh_pr_view_sync(wt_path)
        if pr_info is not None:
            update_worktree_pr_sync(
                repo.name, name, pr_info[0], pr_info[1], db_path=db_path
            )
        imported.append(
            {
                "repo": repo.name,
                "name": name,
                "path": str(wt_path),
                "branch": branch,
                "ticket": row.ticket,
            }
        )

    # Removal pass: any tracked row whose path is no longer in git's
    # output gets dropped. Rows in transient states (setting_up,
    # removing) are owned by an in-flight task and intentionally spared
    # — their path may not be in git yet (setting_up) or is being torn
    # down (removing).
    for name, path in list_worktree_paths_for_repo_sync(repo.name, db_path):
        if path in git_paths:
            continue
        existing = get_worktree_sync(repo.name, name, db_path)
        if existing is None:
            continue
        if existing.status in ("setting_up", "removing"):
            continue
        if delete_worktree_sync(repo.name, name, db_path) > 0:
            removed.append(
                {
                    "repo": repo.name,
                    "name": name,
                    "path": path,
                    "reason": "missing from git worktree list",
                }
            )

    return imported, removed, skipped, relinked


def _gh_pr_view_sync(wt_path: Path) -> tuple[int, str] | None:
    """Shell ``gh pr view --json number,headRepository,headRepositoryOwner``
    in the worktree path. Returns ``(pr_number, "owner/name")`` if a PR
    exists for the branch, ``None`` otherwise.

    Every error path returns None: gh missing, not authed, no PR open
    for the branch, network failure, JSON decode failure, unexpected
    shape. The worktree import succeeds regardless — the pr_state
    poller will retry on its next tick.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                "--json",
                "number,headRepository,headRepositoryOwner",
            ],
            cwd=str(wt_path),
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pr_number = data.get("number")
    head_repo = data.get("headRepository")
    head_owner = data.get("headRepositoryOwner")
    repo_name = (
        head_repo.get("name") if isinstance(head_repo, dict) else None
    )
    owner_login = (
        head_owner.get("login") if isinstance(head_owner, dict) else None
    )
    if (
        not isinstance(pr_number, int)
        or not isinstance(repo_name, str)
        or not isinstance(owner_login, str)
    ):
        return None
    return pr_number, f"{owner_login}/{repo_name}"


def _get_worktree_by_path_sync(
    repo: str, path: str, db_path: Path | None = None
) -> WorktreeRow | None:
    """Path-based lookup. Cheaper than scanning all rows."""
    from app.db import open_db

    if db_path is None:
        db_path = get_db_path()
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT name, branch, ticket, pr_number, pr_repo, created_at, status "
            "FROM worktree WHERE repo = ? AND path = ? LIMIT 1",
            (repo, path),
        ).fetchone()
        if row is None:
            return None
        # The caller checks presence AND inspects pr_number to decide
        # whether an already-tracked row still needs its PR backfilled,
        # so project the real columns rather than a presence sentinel.
        return WorktreeRow(
            repo=repo,
            name=row[0],
            path=path,
            branch=row[1],
            ticket=row[2],
            pr_number=row[3],
            pr_repo=row[4],
            created_at=row[5],
            status=row[6],
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Top-level aggregator
# ---------------------------------------------------------------------------


def sync_all_sync(db_path: Path | None = None) -> dict:
    config = load_config()
    all_imported: list[dict] = []
    all_removed: list[dict] = []
    all_skipped: list[dict] = []
    all_relinked: list[dict] = []
    for repo in config.repos:
        imported, removed, skipped, relinked = sync_worktrees_for_repo_sync(
            repo, db_path
        )
        all_imported.extend(imported)
        all_removed.extend(removed)
        all_skipped.extend(skipped)
        all_relinked.extend(relinked)
    return {
        "imported": all_imported,
        "removed": all_removed,
        "skipped": all_skipped,
        "relinked": all_relinked,
    }
