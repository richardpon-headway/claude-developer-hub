"""Tests for the cached local-user gh login lookup.

The login feeds the hub's REVIEWING-tier split — getting it wrong (or
shelling out per-request) ripples into every workspace list response,
so the cache + fail-open behavior are both worth pinning.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services import gh_identity
from app.services.gh_cli import GhNotFound


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    gh_identity.reset_cache()


async def test_get_user_login_returns_login_from_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``gh api user`` returns the full user object as JSON; we pull
    ``.login`` out of it. Avoid ``--jq .login`` since jq emits bare
    values that aren't JSON, which ``run_gh_json`` would reject."""

    async def fake_run_gh_json(args: list[str], **_: Any) -> dict:
        assert args == ["api", "user"]
        return {"login": "octocat", "id": 1, "name": "Richard"}

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)

    assert await gh_identity.get_user_login() == "octocat"


async def test_get_user_login_caches_after_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def fake_run_gh_json(args: list[str], **_: Any) -> dict:
        calls["n"] += 1
        return {"login": "octocat"}

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)

    await gh_identity.get_user_login()
    await gh_identity.get_user_login()
    await gh_identity.get_user_login()

    # Three calls, but only one shell-out — that's the point of the cache.
    assert calls["n"] == 1


async def test_get_user_login_returns_none_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_gh_json(args: list[str], **_: Any) -> str:
        raise GhNotFound("gh CLI not on PATH")

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)

    assert await gh_identity.get_user_login() is None


async def test_get_user_login_returns_none_on_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_gh_json with swallow_errors=True returns None on failure;
    fail-open here means the REVIEWING tier is disabled but the hub
    keeps working."""
    async def fake_run_gh_json(args: list[str], **_: Any) -> None:
        return None

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)

    assert await gh_identity.get_user_login() is None


async def test_get_user_login_does_not_cache_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient `gh` failure shouldn't poison the cache for the
    rest of the process. The next call should retry the shell-out and
    succeed if `gh` is back."""
    state = {"first_call": True}

    async def fake_run_gh_json(args: list[str], **_: Any) -> dict | None:
        if state["first_call"]:
            state["first_call"] = False
            return None
        return {"login": "octocat"}

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)

    assert await gh_identity.get_user_login() is None
    assert await gh_identity.get_user_login() == "octocat"


async def test_get_user_login_uses_no_jq_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the ``could not parse \`gh api user\` output``
    warning on startup. Earlier code called ``gh api user --jq .login``,
    which emits the bare string ``octocat\\n`` — not valid JSON, so
    ``run_gh_json`` swallowed the result as None and the REVIEWING
    tier silently disabled itself. Pin the args list so future edits
    don't reintroduce the filter."""
    captured = {"args": None}

    async def fake_run_gh_json(args: list[str], **_: Any) -> dict:
        captured["args"] = args
        return {"login": "octocat"}

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)
    await gh_identity.get_user_login()
    assert captured["args"] == ["api", "user"]


async def test_get_user_login_ignores_dict_without_login_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: an unexpected gh payload shape shouldn't poison the
    cache or crash. Treat it the same as a failure — return None and
    let the next call retry."""

    async def fake_run_gh_json(args: list[str], **_: Any) -> dict:
        return {"id": 1, "name": "no-login-key"}

    monkeypatch.setattr(gh_identity, "run_gh_json", fake_run_gh_json)
    assert await gh_identity.get_user_login() is None
