import asyncio

import pytest
from fastapi.testclient import TestClient

import bot
import chat_turn_store


client = TestClient(bot.app)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHAT_LOG_PATH", tmp_path / "chat_history.json")
    monkeypatch.setattr(bot, "BIGQUERY_CHAT_LOG_ENABLED", False)
    monkeypatch.setattr(bot, "CHAT_MIN_SECONDS_BETWEEN_MESSAGES", 0)
    monkeypatch.setattr(chat_turn_store, "CHAT_TURNS_PATH", tmp_path / "chat_turns.json")
    monkeypatch.setattr(chat_turn_store, "BIGQUERY_ENABLED", False)
    chat_turn_store.reset_runtime_state()
    with bot.SESSIONS_LOCK:
        bot.SESSIONS.clear()
    yield
    chat_turn_store.reset_runtime_state()
    with bot.SESSIONS_LOCK:
        bot.SESSIONS.clear()


def _capture_turns(monkeypatch):
    turns = []

    def capture(turn):
        turns.append(dict(turn))
        return dict(turn)

    monkeypatch.setattr(chat_turn_store, "append_turn", capture)
    return turns


def _enable_whatsapp(monkeypatch, statuses):
    queue = list(statuses)
    requests = []

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "fake response"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            requests.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(queue.pop(0) if queue else 200)

    monkeypatch.setattr(bot, "WHATSAPP_TOKEN", "test-token")
    monkeypatch.setattr(bot, "WHATSAPP_PHONE_NUMBER_ID", "phone-id")
    monkeypatch.setattr(bot.httpx, "AsyncClient", FakeAsyncClient)
    return requests


def _webhook_message(message):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


class _FakeContext:
    def get_state(self):
        return None


class _FakeController:
    def __init__(self, decision):
        self.decision = decision

    def create_context(self):
        return _FakeContext()

    def process_message(self, context, message):
        return dict(self.decision)


def test_chat_api_records_user_and_bot_with_options(monkeypatch):
    turns = _capture_turns(monkeypatch)
    monkeypatch.setattr(
        bot,
        "FLOW_CONTROLLER",
        _FakeController({
            "mode": "flow",
            "reply": "Elegí una opción",
            "interactive_type": "button",
            "buttons": [
                {"type": "reply", "reply": {"id": "a", "title": "A"}},
                {"type": "reply", "reply": {"id": "b", "title": "B"}},
            ],
        }),
    )

    response = client.post("/chat", json={"session_id": "api:test", "message": "hola"})

    assert response.status_code == 200
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "botones"]
    assert turns[0]["channel"] == "api"
    assert turns[1]["opciones"] == [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]


def test_chat_api_handoff_orders_user_bot_and_system(monkeypatch):
    turns = _capture_turns(monkeypatch)
    monkeypatch.setattr(
        bot,
        "FLOW_CONTROLLER",
        _FakeController({"mode": "flow", "reply": "Te derivo", "handoff": True}),
    )

    response = client.post("/chat", json={"session_id": "api:handoff", "message": "asesor"})

    assert response.status_code == 200
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "mensaje", "sistema"]
    assert turns[-1]["status"] == "handoff_waiting"


@pytest.mark.parametrize("failing_log", ["legacy", "atomic"])
def test_chat_still_responds_when_logging_raises(monkeypatch, failing_log):
    monkeypatch.setattr(
        bot,
        "FLOW_CONTROLLER",
        _FakeController({"mode": "flow", "reply": "Respuesta disponible"}),
    )
    if failing_log == "legacy":
        monkeypatch.setattr(bot, "_should_use_bigquery_chat_log", lambda: True)
        monkeypatch.setattr(
            bot,
            "_append_chat_log_bigquery",
            lambda _record: (_ for _ in ()).throw(PermissionError("billing disabled")),
        )
    else:
        monkeypatch.setattr(
            chat_turn_store,
            "append_turn",
            lambda _turn: (_ for _ in ()).throw(PermissionError("BigQuery caído")),
        )

    response = client.post(
        "/chat",
        json={"session_id": f"api:{failing_log}", "message": "hola"},
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "Respuesta disponible"


def test_button_reply_and_delivered_buttons_are_atomic_turns(monkeypatch):
    turns = _capture_turns(monkeypatch)
    requests = _enable_whatsapp(monkeypatch, [200])

    def fake_process(req, *, record_turns):
        assert record_turns is False
        return bot.ChatResponse(
            session_id=req.session_id,
            answer="Elegí",
            took_ms=1,
            interactive_type="button",
            buttons=[
                {"type": "reply", "reply": {"id": str(index), "title": f"Opción {index}"}}
                for index in range(1, 5)
            ],
        )

    monkeypatch.setattr(bot, "_process_chat", fake_process)
    response = client.post(
        "/webhook",
        json=_webhook_message({
            "from": "5491112345678",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "si", "title": "Sí"},
            },
        }),
    )

    assert response.status_code == 200
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "botones"]
    assert turns[0]["texto"] == "Sí"
    assert turns[0]["seleccion_opcion"] is True
    assert turns[0]["opcion_id_seleccionada"] == "si"
    assert [option["id"] for option in turns[1]["opciones"]] == ["1", "2", "3"]
    assert len(requests[0]["json"]["interactive"]["action"]["buttons"]) == 3


def test_list_reply_keeps_visible_title_and_selected_row_id(monkeypatch):
    turns = _capture_turns(monkeypatch)
    _enable_whatsapp(monkeypatch, [200])

    monkeypatch.setattr(
        bot,
        "_process_chat",
        lambda req, *, record_turns: bot.ChatResponse(
            session_id=req.session_id,
            answer="Recibido",
            took_ms=1,
        ),
    )
    response = client.post(
        "/webhook",
        json=_webhook_message({
            "from": "5491112345678",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "precios", "title": "No veo los precios"},
            },
        }),
    )

    assert response.status_code == 200
    assert turns[0]["texto"] == "No veo los precios"
    assert turns[0]["seleccion_opcion"] is True
    assert turns[0]["opcion_id_seleccionada"] == "precios"


def test_successful_list_keeps_rows_and_list_name(monkeypatch):
    turns = _capture_turns(monkeypatch)
    _enable_whatsapp(monkeypatch, [200])

    def fake_process(req, *, record_turns):
        return bot.ChatResponse(
            session_id=req.session_id,
            answer="Abrí la lista",
            took_ms=1,
            interactive_type="list",
            list_config={
                "button_text": "Ver opciones",
                "sections": [{
                    "title": "Menú principal",
                    "rows": [
                        {"id": "a", "title": "Opción A", "description": "Detalle"},
                        {"id": "b", "title": "Opción B"},
                    ],
                }],
            },
        )

    monkeypatch.setattr(bot, "_process_chat", fake_process)
    response = client.post(
        "/webhook",
        json=_webhook_message({"from": "5491112345678", "type": "text", "text": {"body": "hola"}}),
    )

    assert response.status_code == 200
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "lista"]
    assert turns[1]["nombre_lista"] == "Menú principal"
    assert turns[1]["opciones"] == [
        {"id": "a", "title": "Opción A", "description": "Detalle"},
        {"id": "b", "title": "Opción B"},
    ]


def test_list_failure_logs_only_delivered_button_fallback(monkeypatch):
    turns = _capture_turns(monkeypatch)
    requests = _enable_whatsapp(monkeypatch, [500, 200])

    def fake_process(req, *, record_turns):
        return bot.ChatResponse(
            session_id=req.session_id,
            answer="Abrí la lista",
            took_ms=1,
            interactive_type="list",
            list_config={
                "button_text": "Ver opciones",
                "sections": [{
                    "title": "Menú",
                    "rows": [
                        {"id": str(index), "title": f"Opción {index}"}
                        for index in range(1, 5)
                    ],
                }],
            },
        )

    monkeypatch.setattr(bot, "_process_chat", fake_process)
    response = client.post(
        "/webhook",
        json=_webhook_message({"from": "5491112345678", "type": "text", "text": {"body": "hola"}}),
    )

    assert response.status_code == 200
    bot_turns = [turn for turn in turns if turn["autor"] == "bot"]
    assert len(bot_turns) == 1
    assert bot_turns[0]["tipo"] == "botones"
    assert [option["id"] for option in bot_turns[0]["opciones"]] == ["1", "2", "3"]
    assert [request["json"]["interactive"]["type"] for request in requests] == ["list", "button"]


def test_webhook_keeps_legacy_chat_history_write_during_transition(monkeypatch):
    turns = _capture_turns(monkeypatch)
    legacy_calls = []
    _enable_whatsapp(monkeypatch, [200])
    monkeypatch.setattr(
        bot,
        "FLOW_CONTROLLER",
        _FakeController({"mode": "flow", "reply": "Respuesta normal"}),
    )
    monkeypatch.setattr(
        bot,
        "_append_chat_log",
        lambda session_id, question, answer: legacy_calls.append((session_id, question, answer)),
    )

    response = client.post(
        "/webhook",
        json=_webhook_message({"from": "5491112345678", "type": "text", "text": {"body": "hola"}}),
    )

    assert response.status_code == 200
    assert len(legacy_calls) == 1
    assert legacy_calls[0][1:] == ("hola", "Respuesta normal")
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "mensaje"]


def test_unsupported_media_records_user_placeholder_and_bot_notice(monkeypatch):
    turns = _capture_turns(monkeypatch)
    _enable_whatsapp(monkeypatch, [200])

    response = client.post(
        "/webhook",
        json=_webhook_message({"from": "5491112345678", "type": "image", "image": {"id": "media"}}),
    )

    assert response.status_code == 200
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "mensaje"]
    assert turns[0]["texto"] == "[MENSAJE NO SOPORTADO: image]"
    assert turns[0]["seleccion_opcion"] is False
    assert turns[1]["autor"] == "bot"


def test_user_during_handoff_is_recorded_without_repeated_system_event(monkeypatch):
    turns = _capture_turns(monkeypatch)
    bot.get_session(f"wa:{bot.normalize_phone('5491112345678')}")["human_handoff"] = True

    response = client.post(
        "/webhook",
        json=_webhook_message({"from": "5491112345678", "type": "text", "text": {"body": "¿Hola?"}}),
    )

    assert response.status_code == 200
    assert len(turns) == 1
    assert turns[0]["tipo"] == "respuesta_usuario"
    assert turns[0]["status"] == "handoff_waiting"


def test_flow_handoff_records_bot_then_single_system_event(monkeypatch):
    turns = _capture_turns(monkeypatch)
    _enable_whatsapp(monkeypatch, [200])

    def fake_process(req, *, record_turns):
        bot.get_session(req.session_id)["human_handoff"] = True
        return bot.ChatResponse(session_id=req.session_id, answer="Te derivo", took_ms=1)

    monkeypatch.setattr(bot, "_process_chat", fake_process)
    response = client.post(
        "/webhook",
        json=_webhook_message({"from": "5491112345678", "type": "text", "text": {"body": "asesor"}}),
    )

    assert response.status_code == 200
    assert [turn["tipo"] for turn in turns] == ["respuesta_usuario", "mensaje", "sistema"]
    assert turns[-1]["status"] == "handoff_waiting"


def test_agent_manual_bot_and_manual_handoff_are_system_turns(monkeypatch):
    turns = _capture_turns(monkeypatch)
    _enable_whatsapp(monkeypatch, [200, 200])

    assert client.post(
        "/agent/send",
        json={"session_id": "wa:5491112345678", "message": "Mensaje del agente"},
    ).status_code == 200
    assert client.post(
        "/bot/send",
        json={"session_id": "wa:5491112345678", "message": "Mensaje manual"},
    ).status_code == 200
    assert client.post(
        "/bot/send",
        json={"session_id": "wa:5491112345678", "handoff": True},
    ).status_code == 200

    assert [turn["tipo"] for turn in turns] == ["sistema", "sistema", "sistema"]
    assert [turn["status"] for turn in turns] == ["agente", "bot_manual", "handoff_waiting"]
    assert all(turn["autor"] == "sistema" for turn in turns)


def test_missing_credentials_and_rejected_send_do_not_create_turn(monkeypatch):
    turns = _capture_turns(monkeypatch)
    monkeypatch.setattr(bot, "WHATSAPP_TOKEN", "")
    monkeypatch.setattr(bot, "WHATSAPP_PHONE_NUMBER_ID", "")

    assert asyncio.run(bot.send_whatsapp_text("5491112345678", "No entregado")) is False
    assert turns == []

    _enable_whatsapp(monkeypatch, [500])
    with pytest.raises(RuntimeError):
        asyncio.run(bot.send_whatsapp_text("5491112345678", "Rechazado"))
    assert turns == []


def test_startup_survives_unexpected_chat_turn_initialization_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(bot, "BIGQUERY_CHAT_LOG_DATASET", "")
    monkeypatch.setattr(bot, "load_company_domains", lambda: ({}, []))
    monkeypatch.setattr(bot, "FlowController", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(bot.message_store, "load_all_messages", lambda: {})
    monkeypatch.setattr(
        bot.chat_turn_store,
        "initialize",
        lambda: (_ for _ in ()).throw(PermissionError("sin billing")),
    )

    asyncio.run(bot.startup())


def test_startup_rejects_gcp_project_without_explicit_dataset(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "consultora-485520")
    monkeypatch.setattr(bot, "BIGQUERY_CHAT_LOG_DATASET", "")

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(bot.startup())

    assert str(exc_info.value) == bot.BIGQUERY_DATASET_REQUIRED_MESSAGE
