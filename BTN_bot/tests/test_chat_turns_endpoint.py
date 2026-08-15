import pytest
from fastapi.testclient import TestClient

import bot
import chat_turn_store


client = TestClient(bot.app)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_turn_store, "CHAT_TURNS_PATH", tmp_path / "chat_turns.json")
    monkeypatch.setattr(chat_turn_store, "BIGQUERY_ENABLED", False)
    chat_turn_store.reset_runtime_state()
    yield
    chat_turn_store.reset_runtime_state()


def _append(session_id, timestamp, *, text="Hola", options=None):
    return chat_turn_store.append_turn({
        "session_id": session_id,
        "timestamp": timestamp,
        "tipo": "botones" if options else "mensaje",
        "autor": "bot",
        "texto": text,
        "opciones": options,
        "interactive_type": "button" if options else None,
        "status": "answered",
        "channel": "whatsapp" if session_id.startswith("wa:") else "api",
    })


def test_returns_envelope_global_order_and_parsed_options():
    _append("wa:b", "2026-08-11T12:00:00+00:00")
    _append(
        "wa:a",
        "2026-08-11T10:00:00+00:00",
        options=[{"id": "si", "title": "Sí"}],
    )
    _append("wa:a", "2026-08-11T11:00:00+00:00")

    response = client.get("/chatlog/turns")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 3
    assert [(turn["session_id"], turn["orden"]) for turn in body["turns"]] == [
        ("wa:a", 0),
        ("wa:a", 1),
        ("wa:b", 0),
    ]
    assert body["turns"][0]["opciones"] == [{"id": "si", "title": "Sí"}]
    assert body["turns"][1]["opciones"] == []


def test_optional_session_and_date_filters_can_be_combined():
    _append("wa:a", "2026-08-10T12:00:00+00:00", text="día 10")
    _append("wa:a", "2026-08-11T12:00:00+00:00", text="día 11")
    _append("wa:b", "2026-08-11T12:00:00+00:00", text="otra sesión")

    response = client.get(
        "/chatlog/turns",
        params={"session_id": "wa:a", "from": "2026-08-11", "to": "2026-08-11"},
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["turns"][0]["texto"] == "día 11"


def test_empty_result_is_successful_envelope():
    response = client.get("/chatlog/turns", params={"session_id": "wa:missing"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "turns": [], "count": 0}


@pytest.mark.parametrize(
    "params, expected_status",
    [
        ({"from": "2026-08-01"}, 422),
        ({"to": "2026-08-01"}, 422),
        ({"from": "01-08-2026", "to": "2026-08-02"}, 422),
        ({"from": "2026-08-02", "to": "2026-08-01"}, 400),
        ({"from": "2026-01-01", "to": "2026-04-01"}, 400),
    ],
)
def test_validates_optional_date_range(params, expected_status):
    response = client.get("/chatlog/turns", params=params)
    assert response.status_code == expected_status


def test_download_restores_same_90_day_validation():
    response = client.get(
        "/chatlog/download",
        params={"from": "2026-01-01", "to": "2026-04-01", "format": "csv"},
    )
    assert response.status_code == 400
    assert "90 días" in response.text


def test_read_failure_returns_503(monkeypatch):
    monkeypatch.setattr(
        chat_turn_store,
        "read_turns",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("sin permisos")),
    )
    response = client.get("/chatlog/turns")
    assert response.status_code == 503
    assert "chat_turns" in response.text
