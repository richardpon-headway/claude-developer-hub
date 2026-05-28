"""Shared helper for shelling out to the GitHub CLI (``gh``).

Centralizes the failure-mode handling every ``gh`` caller in the
codebase has to re-implement otherwise: ``gh`` missing from PATH, the
"no PR found" case, generic non-zero exits, and JSON-decode failures.

Callers fall into two patterns:

- **Polling / background callers** (e.g. ``pr_enrichment_poll``,
  ``inbox_poll``, ``authored_poll``): pass ``swallow_errors=True`` so
  generic failures log and return ``None`` rather than crashing the
  loop.
- **Request handlers** (e.g. ``GET /api/worktree/.../pr-url``): pass
  ``swallow_errors=False`` so generic failures raise :class:`GhFailed`
  which the route layer converts to ``HTTPException(502)``.

Both modes raise :class:`GhNotFound` for the missing/unauthed case so
each caller can decide how to surface it (the polling loop swallows
once and moves on; request handlers turn it into 502).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class GhNotFound(Exception):
    """The ``gh`` CLI isn't on PATH (or isn't authed).

    Distinct from generic failures so callers can surface a specific
    "install GitHub CLI" message instead of leaking raw stderr.
    """


class GhFailed(Exception):
    """``gh`` exited non-zero for a reason other than "no PR found" or
    "gh missing". Carries the trimmed stderr so request-handler callers
    can include it in a 502."""

    def __init__(self, args: list[str], stderr: str) -> None:
        super().__init__(f"`gh {' '.join(args[:2])}` failed: {stderr.strip() or 'unknown error'}")
        self.args = args
        self.stderr = stderr


def _looks_like_gh_missing(stderr_lower: str) -> bool:
    return (
        "executable file not found" in stderr_lower
        or "command not found" in stderr_lower
    )


def _looks_like_no_pr(stderr_lower: str) -> bool:
    return "no pull requests found" in stderr_lower or "no pr found" in stderr_lower


async def run_gh_json(
    args: list[str],
    cwd: Path | None = None,
    *,
    swallow_errors: bool = True,
) -> dict | list | None:
    """Run ``gh <args>`` and return the parsed JSON output.

    Args:
        args: argv for ``gh`` (without the leading ``gh``). Example:
            ``["pr", "view", "--json", "number,title"]``.
        cwd: optional working directory. ``None`` means inherit.
        swallow_errors: if True (polling pattern), generic failures and
            JSON-decode errors are logged and the function returns
            ``None``. If False (request-handler pattern), they raise
            :class:`GhFailed` so the route layer can map to 502.

    Returns:
        Parsed JSON (``dict`` for ``gh pr view``, ``list`` for ``gh
        search prs``, etc.), or ``None`` when:

        - ``gh`` reported "no pull requests found" / "no PR found"
          (the normal "branch hasn't been pushed yet" case).
        - A generic failure occurred and ``swallow_errors=True``.

    Raises:
        GhNotFound: ``gh`` isn't on PATH or isn't authed. Always raised,
            independent of ``swallow_errors``.
        GhFailed: a generic non-zero exit or JSON-decode failure when
            ``swallow_errors=False``.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )
    stdout_b, stderr_b = await proc.communicate()
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        lower = stderr.lower()
        if _looks_like_no_pr(lower):
            return None
        if _looks_like_gh_missing(lower):
            raise GhNotFound("gh CLI not on PATH")
        if swallow_errors:
            log.info(
                "gh %s failed in %s: %s",
                " ".join(args[:2]),
                cwd,
                stderr.strip()[:200],
            )
            return None
        raise GhFailed(args, stderr)

    try:
        return json.loads(stdout_b.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        if swallow_errors:
            log.warning(
                "could not parse `gh %s` output for %s: %s",
                " ".join(args[:2]),
                cwd,
                e,
            )
            return None
        raise GhFailed(args, f"could not parse output: {e}") from e
