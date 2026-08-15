from datetime import datetime, timezone

import pytest

import chat_turn_store
from dev_tools import load_sample_chat_turns


def test_sample_contains_every_visual_variant_and_two_sessions(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("BIGQUERY_CHAT_LOG_ENABLED", raising=False)
    monkeypatch.delenv("CLOUD_RUN", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(chat_turn_store, "BASE_DIR", tmp_path)
    monkeypatch.setattr(chat_turn_store, "CHAT_TURNS_PATH", tmp_path / "logs" / "chat_turns.json")
    chat_turn_store.reset_runtime_state()

    result = load_sample_chat_turns.load_sample_conversation(
        now=datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    )
    turns = chat_turn_store.read_turns(session_id=result["session_id"])

    assert result["count"] == 9
    assert result["new_sessions"] == 2
    assert {turn["tipo"] for turn in turns} == {
        "mensaje",
        "botones",
        "lista",
        "respuesta_usuario",
        "sistema",
    }
    assert {turn["status"] for turn in turns if turn["tipo"] == "sistema"} == {
        "handoff_waiting",
        "agente",
        "bot_manual",
    }
    assert {turn["seleccion_opcion"] for turn in turns if turn["autor"] == "usuario"} == {
        True,
        False,
    }
    assert [turn["orden"] for turn in turns] == list(range(9))


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GOOGLE_CLOUD_PROJECT", "project-prod"),
        ("BIGQUERY_CHAT_LOG_ENABLED", "true"),
        ("CLOUD_RUN", "true"),
        ("K_SERVICE", "service"),
        ("APP_ENV", "production"),
    ],
)
def test_sample_refuses_non_local_environments(variable, value, monkeypatch):
    for key in (
        "GOOGLE_CLOUD_PROJECT",
        "BIGQUERY_CHAT_LOG_ENABLED",
        "CLOUD_RUN",
        "K_SERVICE",
        "APP_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(variable, value)

    with pytest.raises(RuntimeError, match="solo puede ejecutarse en desarrollo local"):
        load_sample_chat_turns.assert_local_execution()
