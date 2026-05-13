"""Tests for the /api/token-usage proxy."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "cdh-test.db"
    config_path = tmp_path / "cdh-test.yaml"
    monkeypatch.setenv("CDH_DB_PATH", str(db_path))
    monkeypatch.setenv("CDH_CONFIG_PATH", str(config_path))
    db.apply_migrations_sync(db_path)
    # Minimal config; the proxy reads token_monitor.api_url.
    config_path.write_text(
        yaml.safe_dump(
            {
                "development_root": str(tmp_path),
                "repos": [],
                "token_monitor": {
                    "api_url": "http://localhost:47821",
                    "sidecar_dir": str(tmp_path / "sidecars"),
                },
            }
        )
    )
    return {"db_path": db_path, "config_path": config_path}


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, get_impl) -> None:
    """Replace httpx.AsyncClient with a context manager whose .get returns
    the supplied callable's value (sync) or raises if it raises.

    The proxy now hits two CTM URLs in parallel (``/api/usage/windows``
    and ``/api/usage/groups``); ``get_impl`` receives the URL so tests
    can dispatch per-endpoint or treat both identically.
    """

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return get_impl(url)

    monkeypatch.setattr("app.routes.token_usage.httpx.AsyncClient", _FakeClient)


def _mock_response(payload: dict, raise_exc: Exception | None = None) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=raise_exc)
    response.json = MagicMock(return_value=payload)
    return response


_OFFLINE_BODY = {
    "offline": True,
    "today_output": 0,
    "today_input": 0,
    "today_messages": 0,
    "rows": [],
}


def test_token_usage_returns_offline_when_ctm_unreachable(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    def boom(url):
        raise httpx.ConnectError("connection refused")

    _patch_httpx(monkeypatch, boom)
    with TestClient(app) as client:
        r = client.get("/api/token-usage")
    assert r.status_code == 200
    assert r.json() == _OFFLINE_BODY


def test_token_usage_trims_sample_prompts(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    def dispatch(url):
        if "/windows" in url:
            return _mock_response(
                {
                    "today_local": {
                        "output": 12_345,
                        "input": 67_890,
                        "messages": 42,
                    }
                }
            )
        return _mock_response(
            {
                "by": "topic",
                "range": "1d",
                "rows": [
                    {
                        "topic_id": "PROJ-1",
                        "sessions": 3,
                        "output": 5000,
                        "input": 200000,
                        "messages": 12,
                        "last_at": "2026-05-12T00:00:00+00:00",
                        "label": "PROJ-1",
                        "summary": "fix the thing",
                        "sample_prompts": ["x" * 10000, "y" * 10000],
                    },
                ],
            }
        )

    _patch_httpx(monkeypatch, dispatch)
    with TestClient(app) as client:
        r = client.get("/api/token-usage")
    assert r.status_code == 200
    body = r.json()
    assert body["offline"] is False
    assert body["today_output"] == 12_345
    assert body["today_input"] == 67_890
    assert body["today_messages"] == 42
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["topic_id"] == "PROJ-1"
    assert row["output"] == 5000
    assert row["label"] == "PROJ-1"
    # sample_prompts is stripped
    assert "sample_prompts" not in row


def test_token_usage_handles_missing_rows_key(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    def dispatch(url):
        if "/windows" in url:
            return _mock_response({"today_local": {"output": 0, "input": 0, "messages": 0}})
        return _mock_response({"by": "topic", "range": "1d"})

    _patch_httpx(monkeypatch, dispatch)
    with TestClient(app) as client:
        r = client.get("/api/token-usage")
    assert r.status_code == 200
    body = r.json()
    assert body["offline"] is False
    assert body["rows"] == []
    assert body["today_output"] == 0


def test_token_usage_offline_on_http_error(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    def http_500(url):
        return _mock_response(
            {},
            raise_exc=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock(status_code=500)
            ),
        )

    _patch_httpx(monkeypatch, http_500)
    with TestClient(app) as client:
        r = client.get("/api/token-usage")
    assert r.status_code == 200
    assert r.json() == _OFFLINE_BODY


def test_token_usage_offline_when_only_one_endpoint_fails(
    monkeypatch: pytest.MonkeyPatch, _isolate: dict[str, Path]
) -> None:
    """If the windows call succeeds but groups fails (or vice versa),
    the whole tile reports offline. Simpler than partial state."""

    def dispatch(url):
        if "/windows" in url:
            return _mock_response(
                {"today_local": {"output": 100, "input": 0, "messages": 1}}
            )
        raise httpx.ConnectError("groups endpoint unreachable")

    _patch_httpx(monkeypatch, dispatch)
    with TestClient(app) as client:
        r = client.get("/api/token-usage")
    assert r.status_code == 200
    assert r.json() == _OFFLINE_BODY
