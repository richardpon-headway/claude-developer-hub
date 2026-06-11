"""Tests for the todo widget routes + store."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def _create(client: TestClient, **body: object) -> dict:
    r = client.post("/api/widgets/todo/items", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_list_empty(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.get("/api/widgets/todo/items")
    assert r.status_code == 200
    assert r.json() == {"pending": [], "completed": []}


def test_create_defaults_to_empty_pending_card(
    _isolate: dict[str, Path],
) -> None:
    with TestClient(app) as client:
        item = _create(client)
        assert item["title"] == ""
        assert item["bullets"] == []
        assert item["done"] is False
        assert item["completed_at"] is None

        listing = client.get("/api/widgets/todo/items").json()
    assert [i["id"] for i in listing["pending"]] == [item["id"]]
    assert listing["completed"] == []


def test_create_with_title_and_bullets(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        item = _create(client, title="ship the widget", bullets=["write tests", "wire UI"])
    assert item["title"] == "ship the widget"
    assert item["bullets"] == ["write tests", "wire UI"]


def test_patch_autosaves_title_and_bullets(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        item = _create(client)
        r = client.patch(
            f"/api/widgets/todo/items/{item['id']}",
            json={"title": "renamed", "bullets": ["a", "b"]},
        )
        assert r.status_code == 200
        updated = r.json()
    assert updated["title"] == "renamed"
    assert updated["bullets"] == ["a", "b"]


def test_complete_moves_item_to_completed_section(
    _isolate: dict[str, Path],
) -> None:
    with TestClient(app) as client:
        item = _create(client, title="finish me")
        r = client.patch(f"/api/widgets/todo/items/{item['id']}", json={"done": True})
        assert r.status_code == 200
        assert r.json()["done"] is True
        assert r.json()["completed_at"] is not None

        listing = client.get("/api/widgets/todo/items").json()
    assert listing["pending"] == []
    assert [i["id"] for i in listing["completed"]] == [item["id"]]


def test_uncomplete_returns_item_to_bottom_of_pending(
    _isolate: dict[str, Path],
) -> None:
    with TestClient(app) as client:
        first = _create(client, title="first")
        second = _create(client, title="second")
        # Complete `first`, then re-open it — it should land below
        # `second` rather than reclaiming its original top slot.
        client.patch(f"/api/widgets/todo/items/{first['id']}", json={"done": True})
        client.patch(f"/api/widgets/todo/items/{first['id']}", json={"done": False})
        listing = client.get("/api/widgets/todo/items").json()
    assert [i["id"] for i in listing["pending"]] == [second["id"], first["id"]]
    assert listing["completed"] == []


def test_reorder_rewrites_pending_order(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        a = _create(client, title="a")
        b = _create(client, title="b")
        c = _create(client, title="c")
        r = client.post(
            "/api/widgets/todo/reorder",
            json={"ids": [c["id"], a["id"], b["id"]]},
        )
        assert r.status_code == 200
        listing = r.json()
    assert [i["id"] for i in listing["pending"]] == [
        c["id"],
        a["id"],
        b["id"],
    ]


def test_delete_removes_item(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        item = _create(client, title="temporary")
        r = client.delete(f"/api/widgets/todo/items/{item['id']}")
        assert r.status_code == 200
        assert r.json() == {"deleted": True}
        listing = client.get("/api/widgets/todo/items").json()
    assert listing == {"pending": [], "completed": []}


def test_patch_unknown_id_404(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.patch("/api/widgets/todo/items/9999", json={"title": "x"})
    assert r.status_code == 404


def test_delete_unknown_id_404(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.delete("/api/widgets/todo/items/9999")
    assert r.status_code == 404


def test_bullets_round_trip_as_list(_isolate: dict[str, Path]) -> None:
    """Bullets are stored as JSON but always surface as a real list."""
    with TestClient(app) as client:
        item = _create(client, title="t", bullets=["one", "two", "three"])
        fetched = client.get("/api/widgets/todo/items").json()["pending"][0]
    assert fetched["bullets"] == ["one", "two", "three"]
    assert item["bullets"] == ["one", "two", "three"]
