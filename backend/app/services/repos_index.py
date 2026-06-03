"""Match a PR's ``owner/name`` to a configured :class:`RepoConfig`.

Used wherever the app needs to decide "is this PR's repo one the user
has onboarded?" — the authored-PR list, the bookmark guard, and the
pull-down engine. Built once per request/poll tick from
``config.repos``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ReposIndex:
    """Resolved lookup tables for matching a PR's ``pr_repo`` to a
    configured :class:`RepoConfig`.

    - ``by_github_repo``: explicit ``owner/name`` mapping for repos
      that declare ``github_repo``. Authoritative when present.
    - ``by_basename``: fallback mapping keyed on ``RepoConfig.name``
      (which usually equals the GitHub repo basename). Lets configs
      that haven't been retrofitted with ``github_repo`` still
      participate in matching.
    """

    by_github_repo: dict[str, Any]
    by_basename: dict[str, Any]


def configured_repos_index(repos: list[Any]) -> ReposIndex:
    by_github_repo: dict[str, Any] = {}
    by_basename: dict[str, Any] = {}
    for repo in repos:
        if getattr(repo, "github_repo", None):
            by_github_repo[repo.github_repo] = repo
        by_basename[repo.name] = repo
    return ReposIndex(by_github_repo=by_github_repo, by_basename=by_basename)


def lookup_configured_repo(pr_repo: str, index: ReposIndex) -> Any | None:
    """Return the :class:`RepoConfig` matching ``pr_repo``, preferring
    the explicit ``github_repo`` mapping, falling back to a basename
    match against ``RepoConfig.name`` ONLY for repos that haven't
    declared ``github_repo``. Returns ``None`` if nothing matches.

    Excluding repos that have ``github_repo`` set from the basename
    fallback prevents a misleading "configured" match: if the user
    explicitly told CDH "this repo is ``corp/myapp``", a PR from
    ``acme/myapp`` shouldn't piggy-back on that config — the user
    opted into precision when they set the field.
    """
    if pr_repo in index.by_github_repo:
        return index.by_github_repo[pr_repo]
    parts = pr_repo.split("/", 1)
    if len(parts) != 2:
        return None
    candidate = index.by_basename.get(parts[1])
    if candidate is None or getattr(candidate, "github_repo", None) is not None:
        return None
    return candidate


def is_repo_configured(pr_repo: str, index: ReposIndex) -> bool:
    return lookup_configured_repo(pr_repo, index) is not None
