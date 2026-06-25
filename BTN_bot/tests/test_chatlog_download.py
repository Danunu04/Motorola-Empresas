"""
Tests de GET /chatlog/download.

Ejecutar:
    pytest tests/test_chatlog_download.py
"""

import csv
import io

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
    import json
    with open(bot.CHAT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


class TestValidation:
    def test_missing_params_returns_422(self):
        resp = client.get("/chatlog/download")
        assert resp.status_code == 422

    def test_from_after_to_returns_400(self):
        resp = client.get("/chatlog/download", params={"from": "2026-06-10", "to": "2026-06-01"})
        assert resp.status_code == 400

    def test_range_over_90_days_returns_400(self):
        resp = client.get("/chatlog/download", params={"from": "2026-01-01", "to": "2026-06-01"})
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
        _seed_record("2026-06-10T12:00:00+00:00")
        resp = client.get(
            "/chatlog/download",
            params={"from": "2026-06-01", "to": "2026-06-20", "format": "pdf"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert "attachment" in resp.headers["content-disposition"]
        assert len(resp.content) > 0
