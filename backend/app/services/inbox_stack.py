"""Stack detection over a list of inbox PRs.

A "stack" is a chain where PR A's ``baseRefName`` matches PR B's
``headRefName`` (both PRs in the same repo). Walking the chain gives
the top-of-stack PR and each member's position.

This runs entirely over the in-process search results — no Graphite
API call, no extra ``gh`` round-trips. Cross-author stacks are picked
up as long as both PRs landed in the search results; cross-repo
stacks aren't a real concept on GitHub and aren't detected.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.inbox_search import InboxPrRaw


@dataclass
class StackAnnotation:
    """Computed per PR. Single-PR results get ``stack_size=1``,
    ``stack_position=1``, ``stack_top_pr_number=None``."""

    stack_top_pr_number: int | None
    stack_size: int
    stack_position: int  # 1 = bottom (closest to main); N = top of stack


def annotate_stacks(prs: list[InboxPrRaw]) -> dict[tuple[str, int], StackAnnotation]:
    """Return a mapping of ``(pr_repo, pr_number)`` to its annotation.

    Algorithm:
    1. Group PRs by ``pr_repo`` — stacks never cross repos.
    2. Within each repo, build maps: ``head_ref -> pr`` and ``base_ref ->
       children``.
    3. A PR is the "top of a stack" if no other PR in the same repo has
       ``base_ref == this.head_ref`` — i.e. no child stacks on top.
    4. Walk down from each top via ``base_ref`` lookups to enumerate the
       stack's members in order (top → bottom). Reverse so position 1
       is the bottom (closest to main).
    """
    out: dict[tuple[str, int], StackAnnotation] = {}

    by_repo: dict[str, list[InboxPrRaw]] = {}
    for p in prs:
        by_repo.setdefault(p.pr_repo, []).append(p)

    for repo, repo_prs in by_repo.items():
        head_to_pr: dict[str, InboxPrRaw] = {p.head_ref: p for p in repo_prs}
        # Set of head_refs that some other PR in this repo bases on —
        # those PRs are NOT stack tops.
        bases_used: set[str] = {p.base_ref for p in repo_prs}

        tops = [p for p in repo_prs if p.head_ref not in bases_used]
        for top in tops:
            # Walk from `top` down via base_ref → next PR's head_ref.
            chain: list[InboxPrRaw] = [top]
            cursor = top
            visited: set[int] = {top.pr_number}
            while True:
                next_pr = head_to_pr.get(cursor.base_ref)
                if next_pr is None or next_pr.pr_number in visited:
                    break
                visited.add(next_pr.pr_number)
                chain.append(next_pr)
                cursor = next_pr

            # chain is ordered top → bottom; reverse so position 1 is
            # the bottom (closest to main).
            chain_bottom_first = list(reversed(chain))
            stack_size = len(chain_bottom_first)
            stack_top_pr_number = top.pr_number if stack_size > 1 else None

            for idx, pr in enumerate(chain_bottom_first, start=1):
                out[(repo, pr.pr_number)] = StackAnnotation(
                    stack_top_pr_number=stack_top_pr_number,
                    stack_size=stack_size,
                    stack_position=idx,
                )

    # Any PR that didn't fall into a top-walk (orphans, cycles) gets
    # a default annotation so callers don't have to special-case None.
    for p in prs:
        out.setdefault(
            (p.pr_repo, p.pr_number),
            StackAnnotation(
                stack_top_pr_number=None, stack_size=1, stack_position=1
            ),
        )

    return out
