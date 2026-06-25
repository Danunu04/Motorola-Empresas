"""
Tests de los endpoints GET/PUT /messages y POST /messages/{key}/reset.

Ejecutar:
    pytest tests/test_messages_endpoints.py
"""

import pytest
from fastapi.testclient import TestClient

import message_store
from bot import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    message_store.invalidate_cache()
    yield
    message_store.invalidate_cache()


class TestListMessages:
    def test_returns_seeded_defaults(self):
        resp = client.get("/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["messages"]
        assert all(item["source"] == "default" for item in body["messages"])


class TestUpdateMessage:
    def test_unknown_key_returns_404(self):
        resp = client.put("/messages/no_existe", json={"content": "x", "updated_by": "qa"})
        assert resp.status_code == 404

    def test_blank_content_returns_422(self):
        resp = client.put("/messages/welcome_message", json={"content": "   ", "updated_by": "qa"})
        assert resp.status_code == 422

    def test_content_over_limit_returns_422(self):
        # main_menu_row_registro_title is a list_row_title (limit: 24 chars per contexto.md §4).
        resp = client.put(
            "/messages/main_menu_row_registro_title",
            json={"content": "x" * 25, "updated_by": "qa"},
        )
        assert resp.status_code == 422

    def test_valid_content_without_bigquery_returns_503(self):
        resp = client.put(
            "/messages/welcome_message",
            json={"content": "Hola!", "updated_by": "qa"},
        )
        assert resp.status_code == 503


class TestResetMessage:
    def test_unknown_key_returns_404(self):
        resp = client.post("/messages/no_existe/reset", json={"updated_by": "qa"})
        assert resp.status_code == 404

    def test_known_key_without_bigquery_returns_503(self):
        resp = client.post("/messages/welcome_message/reset", json={"updated_by": "qa"})
        assert resp.status_code == 503
