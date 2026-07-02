"""
Tests de GET /chatlog/download.

Ejecutar:
    pytest tests/test_chatlog_download.py
"""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

import bot
from bot import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_chat_log(tmp_path, monkeypatch):
    log_path = tmp_path / "chat_log.json"
    monkeypatch.setattr(bot, "CHAT_LOG_PATH", log_path)
    monkeypatch.setattr(bot, "BIGQUERY_CHAT_LOG_ENABLED", False)
    yield log_path


def _seed_record(timestamp: str):
    bot._append_chat_log("api:session-1", "hola", "respuesta")
    # _append_chat_log stamps "now" as the timestamp; overwrite it with a known date.
    logs = bot._load_chat_log()
    logs[-1]["timestamp"] = timestamp
    bot.CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(bot.CHAT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


def _seed_local_record(session_id: str, question: str, answer: str, timestamp: str):
    record = bot._normalize_chat_log_record(
        {"session_id": session_id, "question": question, "answer": answer}
    )
    record["timestamp"] = timestamp
    logs = bot._load_chat_log()
    logs.append(record)
    bot.CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(bot.CHAT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


class TestValidation:
    def test_missing_params_returns_422(self):
        resp = client.get("/chatlog/download")
        assert resp.status_code == 422

    def test_from_after_to_returns_400(self):
        resp = client.get("/chatlog/download", params={"from": "2026-06-10", "to": "2026-06-01"})
        assert resp.status_code == 400

    def test_invalid_format_returns_422(self):
        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-02", "format": "xml"},
        )
        assert resp.status_code == 422

    def test_no_data_in_range_returns_404(self):
        resp = client.get("/chatlog/download", params={"from": "2026-06-01", "to": "2026-06-02"})
        assert resp.status_code == 404


class TestDownloadFormats:
    def test_csv_contains_header_and_record(self):
        _seed_record("2026-06-10T12:00:00+00:00")
        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "csv"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]

        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        assert rows[0] == bot.CHATLOG_COLUMNS
        assert len(rows) == 2

    def test_json_contains_record_with_8_columns(self):
        _seed_record("2026-06-10T12:00:00+00:00")
        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "json"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert len(body) == 1
        assert set(body[0].keys()) == set(bot.CHATLOG_COLUMNS)

    def test_pdf_returns_non_empty_bytes(self):
        pytest.importorskip("reportlab")
        _seed_record("2026-06-10T12:00:00+00:00")
        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "pdf"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert "attachment" in resp.headers["content-disposition"]
        assert len(resp.content) > 0


class TestTxtFormat:
    def test_txt_requires_bigquery_when_not_configured(self):
        _seed_record("2026-06-10T12:00:00+00:00")
        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "txt"},
        )
        assert resp.status_code == 503
        assert "BigQuery" in resp.text

    def test_txt_format_with_mocked_bigquery(self, monkeypatch):
        fake_logs = [
            bot._normalize_chat_log_record(
                {
                    "session_id": "api:session-1",
                    "question": "hola",
                    "answer": "respuesta",
                    "timestamp": "2026-06-10T12:00:00+00:00",
                }
            )
        ]
        calls = {}

        def fake_read(start, end, session_id=None):
            calls["session_id"] = session_id
            return fake_logs

        monkeypatch.setattr(bot, "_should_use_bigquery_chat_log", lambda: True)
        monkeypatch.setattr(bot, "_read_chat_log_bigquery_range", fake_read)

        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "txt"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert ".txt" in resp.headers["content-disposition"]
        assert "Pregunta: hola" in resp.text
        assert "Respuesta: respuesta" in resp.text
        assert "session_id" not in resp.text
        assert "channel" not in resp.text

    def test_txt_groups_by_session_with_mocked_bigquery(self, monkeypatch):
        fake_logs = [
            bot._normalize_chat_log_record(
                {
                    "session_id": "api:session-1",
                    "question": "hola",
                    "answer": "respuesta",
                    "timestamp": "2026-06-10T12:00:00+00:00",
                    "channel": "api",
                    "environment": "cloud_run",
                }
            )
        ]
        monkeypatch.setattr(bot, "_should_use_bigquery_chat_log", lambda: True)
        monkeypatch.setattr(bot, "_read_chat_log_bigquery_range", lambda _s, _e, session_id=None: fake_logs)

        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "txt"},
        )
        assert resp.status_code == 200
        assert "Chat 1:" in resp.text
        assert "api:session-1" in resp.text
        assert "Pregunta: hola" in resp.text
        assert "Respuesta: respuesta" in resp.text
        assert "cloud_run" not in resp.text


class TestSessionIdFiltering:
    def test_csv_filters_by_session_id_locally(self):
        _seed_local_record("api:session-a", "hola a", "respuesta a", "2026-06-10T12:00:00+00:00")
        _seed_local_record("api:session-b", "hola b", "respuesta b", "2026-06-10T13:00:00+00:00")

        resp = client.get(
            "/chatlog/download",
            params={
                "from": "2026-06-01",
                "to": "2026-06-20",
                "format": "csv",
                "session_id": "api:session-a",
            },
        )
        assert resp.status_code == 200
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "api:session-a"
        assert rows[0]["question"] == "hola a"

    def test_session_id_passed_to_bigquery_helper(self, monkeypatch):
        calls = {}

        def fake_read(start, end, session_id=None):
            calls["session_id"] = session_id
            return []

        monkeypatch.setattr(bot, "_should_use_bigquery_chat_log", lambda: True)
        monkeypatch.setattr(bot, "_read_chat_log_bigquery_range", fake_read)

        resp = client.get(
            "/chatlog/download",
            params={
                "from": "2026-06-01",
                "to": "2026-06-20",
                "format": "csv",
                "session_id": "api:session-a",
            },
        )
        assert resp.status_code == 404
        assert calls.get("session_id") == "api:session-a"

    def test_session_id_no_match_returns_404(self):
        _seed_local_record("api:session-a", "hola", "respuesta", "2026-06-10T12:00:00+00:00")
        resp = client.get(
            "/chatlog/download",
            params={
                "from": "2026-06-01",
                "to": "2026-06-20",
                "format": "csv",
                "session_id": "api:session-z",
            },
        )
        assert resp.status_code == 404
