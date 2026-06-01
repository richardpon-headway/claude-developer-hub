"""Shared async helpers for shelling out to ``git`` inside a worktree
or repo checkout.

Covers the small handful of git subprocess calls the file-view endpoint
and the worktree-create flow need:

- ``current_branch``: name of HEAD's branch (or ``None`` for detached).
- ``merge_base``: SHA of the common ancestor with a base ref.
- ``diff_against_ref``: parsed unified-diff hunks for one file vs a ref.
- ``rename_source``: the old path when this file is a rename in the
  branch's history.
- ``list_git_worktrees``: parsed ``git worktree list --porcelain`` for
  a repo checkout — used by ``create_worktree`` to detect a worktree
  that a prior killed attempt already registered.

All functions take a path as ``wt_path`` / ``repo_path`` and run git with
``-C <path>``. Failures return empty / ``None`` rather than raise —
the file-view endpoint prefers to render a degraded UI (no diff
overlay) over a 5xx, since git can legitimately fail for valid reasons
(detached HEAD, base ref missing locally, file untracked, etc.); the
worktree-create flow falls back on ``git worktree add`` exit codes to
report real problems.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitDiffLine:
    """One line of a unified diff body."""

    kind: str  # "context" | "add" | "remove"
    content: str
    # 1-indexed position in the post-image file (target side of the
    # diff). None for "remove" lines, since they don't exist in the
    # target file.
    new_lineno: int | None
    # 1-indexed position in the pre-image file (source side of the
    # diff). None for "add" lines.
    old_lineno: int | None


@dataclass(frozen=True)
class GitDiffHunk:
    """One ``@@ -a,b +c,d @@`` hunk plus its body lines."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[GitDiffLine]


async def _run_git(
    wt_path: Path, args: list[str]
) -> tuple[int, bytes, bytes]:
    """Shell ``git -C <wt_path> <args>``. Returns ``(returncode, stdout,
    stderr)``. Returns ``(-1, b"", b"")`` when git is not on PATH —
    callers treat that the same as a non-zero exit (degrade gracefully).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(wt_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("git not on PATH; file-view degraded")
        return -1, b"", b""
    stdout_b, stderr_b = await proc.communicate()
    return proc.returncode or 0, stdout_b, stderr_b


async def current_branch(wt_path: Path) -> str | None:
    """Return the short branch name for HEAD, or ``None`` on detached
    HEAD or any git failure."""
    rc, out, _ = await _run_git(wt_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        return None
    name = out.decode("utf-8", errors="replace").strip()
    if not name or name == "HEAD":
        return None
    return name


async def merge_base(wt_path: Path, base_ref: str) -> str | None:
    """Return ``git merge-base HEAD <base_ref>`` SHA, or ``None`` when
    the ref doesn't resolve or HEAD has no shared ancestor with it."""
    rc, out, _ = await _run_git(wt_path, ["merge-base", "HEAD", base_ref])
    if rc != 0:
        return None
    sha = out.decode("utf-8", errors="replace").strip()
    return sha or None


async def resolve_base_ref(wt_path: Path, default_branch: str) -> str | None:
    """Find a usable base ref for diff comparisons.

    Tries ``origin/<default_branch>`` first (closer to what GitHub sees),
    falls back to the bare ``<default_branch>`` ref. Returns the ref
    name that resolved, or ``None`` when neither does.
    """
    for ref in (f"origin/{default_branch}", default_branch):
        rc, _, _ = await _run_git(wt_path, ["rev-parse", "--verify", "--quiet", ref])
        if rc == 0:
            return ref
    return None


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def parse_unified_diff(diff_text: str) -> list[GitDiffHunk]:
    """Parse the body of ``git diff`` output (one file) into hunks.

    Assumes the input is the output of ``git diff`` for a single path
    (the ``diff --git`` / ``+++`` / ``---`` headers are tolerated and
    skipped). Each hunk yields its ``@@`` header plus the body lines
    with kind ∈ {context, add, remove}.
    """
    hunks: list[GitDiffHunk] = []
    current: GitDiffHunk | None = None
    old_lineno = 0
    new_lineno = 0
    in_hunk = False
    for raw in diff_text.splitlines():
        if raw.startswith("@@"):
            m = _HUNK_HEADER.match(raw)
            if not m:
                in_hunk = False
                continue
            old_start = int(m.group("old_start"))
            old_count = int(m.group("old_count") or 1)
            new_start = int(m.group("new_start"))
            new_count = int(m.group("new_count") or 1)
            current = GitDiffHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=[],
            )
            hunks.append(current)
            old_lineno = old_start
            new_lineno = new_start
            in_hunk = True
            continue
        if not in_hunk or current is None:
            continue
        # Skip "\ No newline at end of file" sentinel.
        if raw.startswith("\\"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current.lines.append(
                GitDiffLine(
                    kind="add",
                    content=raw[1:],
                    new_lineno=new_lineno,
                    old_lineno=None,
                )
            )
            new_lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            current.lines.append(
                GitDiffLine(
                    kind="remove",
                    content=raw[1:],
                    new_lineno=None,
                    old_lineno=old_lineno,
                )
            )
            old_lineno += 1
        elif raw.startswith(" "):
            current.lines.append(
                GitDiffLine(
                    kind="context",
                    content=raw[1:],
                    new_lineno=new_lineno,
                    old_lineno=old_lineno,
                )
            )
            old_lineno += 1
            new_lineno += 1
        # Anything else (blank line between hunks etc.) is ignored.
    return hunks


async def diff_against_ref(
    wt_path: Path, ref: str, rel_path: Path
) -> list[GitDiffHunk]:
    """Return parsed unified-diff hunks for ``rel_path`` between
    ``ref`` and the working tree (i.e. ``git diff <ref> -- <path>``).

    Use ``ref="HEAD"`` for uncommitted changes (working tree vs HEAD),
    or the merge-base SHA for committed changes vs the branch base.

    Returns an empty list on git failure or when the file is unchanged
    against the ref.
    """
    rc, out, _ = await _run_git(
        wt_path,
        [
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--unified=0",
            ref,
            "--",
            str(rel_path),
        ],
    )
    if rc != 0:
        return []
    return parse_unified_diff(out.decode("utf-8", errors="replace"))


async def diff_against_ref_untracked(
    wt_path: Path, rel_path: Path
) -> list[GitDiffHunk]:
    """Synthesize an "all-added" hunk for an untracked file.

    ``git diff HEAD`` doesn't include untracked files. For the
    file-view's uncommitted overlay, we want every line of a new-but-
    not-yet-staged file to render as uncommitted-add. Read the file
    and emit one big hunk.
    """
    full = wt_path / rel_path
    try:
        text = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    body_lines = text.splitlines()
    if not body_lines:
        return []
    hunk = GitDiffHunk(
        old_start=0,
        old_count=0,
        new_start=1,
        new_count=len(body_lines),
        lines=[
            GitDiffLine(
                kind="add",
                content=line,
                new_lineno=i + 1,
                old_lineno=None,
            )
            for i, line in enumerate(body_lines)
        ],
    )
    return [hunk]


async def is_tracked(wt_path: Path, rel_path: Path) -> bool:
    """``git ls-files --error-unmatch <path>`` exits 0 iff the path is
    tracked. Used to decide between ``git diff HEAD`` (tracked) and the
    synthesized all-added hunk (untracked)."""
    rc, _, _ = await _run_git(
        wt_path,
        ["ls-files", "--error-unmatch", "--", str(rel_path)],
    )
    return rc == 0


@dataclass(frozen=True)
class GitWorktree:
    """One row from ``git worktree list --porcelain``.

    ``branch`` is ``None`` for detached-HEAD / bare worktrees (the
    porcelain record omits the ``branch`` line in those cases).
    """

    path: str
    branch: str | None
    detached: bool
    locked: bool
    prunable: bool


async def list_git_worktrees(repo_path: Path) -> list[GitWorktree]:
    """Parsed output of ``git worktree list --porcelain`` run inside
    ``repo_path``. Returns an empty list on any git failure — callers
    treat "couldn't list" the same as "nothing pre-existing." Used by
    ``create_worktree`` to recognize a worktree that a prior killed
    attempt already registered.

    The parser is shared with the sync ``worktree_import`` flow (see
    ``parse_worktree_list_porcelain``); only the subprocess wrapper
    differs (async here, sync there).
    """
    # Local import — ``worktree_import`` doesn't import this module, so
    # there's no cycle, but the function-level import keeps the module
    # load order obvious.
    from app.services.worktree_import import (
        _branch_from_record,
        parse_worktree_list_porcelain,
    )

    rc, out, _ = await _run_git(repo_path, ["worktree", "list", "--porcelain"])
    if rc != 0:
        return []
    out_text = out.decode("utf-8", errors="replace")
    result: list[GitWorktree] = []
    for rec in parse_worktree_list_porcelain(out_text):
        path = rec.get("worktree")
        if not isinstance(path, str):
            continue
        result.append(
            GitWorktree(
                path=path,
                branch=_branch_from_record(rec),
                detached=bool(rec.get("detached")),
                locked=bool(rec.get("locked")),
                prunable=bool(rec.get("prunable")),
            )
        )
    return result


async def rename_source(
    wt_path: Path, base_ref: str, rel_path: Path
) -> str | None:
    """Detect whether ``rel_path`` was renamed in the branch's history
    relative to ``base_ref``. Returns the old path on rename, ``None``
    otherwise.

    Uses ``git diff --name-status -M <base_ref>..HEAD`` and looks for a
    ``R<sim>\\told\\tnew`` row whose ``new`` matches ``rel_path``.
    """
    rc, out, _ = await _run_git(
        wt_path,
        ["diff", "--name-status", "-M", "--no-color", f"{base_ref}..HEAD"],
    )
    if rc != 0:
        return None
    target = str(rel_path)
    for line in out.decode("utf-8", errors="replace").splitlines():
        # Format: R100\told\tnew  (or C<sim> for copy — ignore copies).
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, old_path, new_path = parts
        if status.startswith("R") and new_path == target:
            return old_path
    return None
