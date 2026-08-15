import json
from datetime import datetime, timezone

import pytest

import chat_turn_store


@pytest.fixture(autouse=True)
def explicit_test_dataset(monkeypatch):
    monkeypatch.setattr(chat_turn_store, "BIGQUERY_DATASET", "unit_test_dataset")


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_turn_store, "CHAT_TURNS_PATH", tmp_path / "chat_turns.json")
    monkeypatch.setattr(chat_turn_store, "BIGQUERY_ENABLED", False)
    chat_turn_store.reset_runtime_state()
    yield tmp_path / "chat_turns.json"
    chat_turn_store.reset_runtime_state()


def _turn(session_id="api:test", timestamp=None, **overrides):
    payload = {
        "session_id": session_id,
        "timestamp": timestamp or "2026-08-11T12:00:00+00:00",
        "tipo": "mensaje",
        "autor": "bot",
        "texto": "Hola",
        "channel": "api",
    }
    payload.update(overrides)
    return payload


def test_schema_has_17_fields_without_author_name():
    assert len(chat_turn_store.CHAT_TURN_FIELDS) == 17
    assert "autor_nombre" not in chat_turn_store.CHAT_TURN_FIELDS
    if chat_turn_store.bigquery is not None:
        assert [field.name for field in chat_turn_store._schema()] == chat_turn_store.CHAT_TURN_FIELDS


def test_bigquery_table_id_rejects_missing_explicit_dataset(monkeypatch):
    monkeypatch.setattr(chat_turn_store, "BIGQUERY_DATASET", "")

    with pytest.raises(RuntimeError) as exc_info:
        chat_turn_store._table_id(type("Client", (), {"project": "consultora-485520"})())

    assert str(exc_info.value) == chat_turn_store.BIGQUERY_DATASET_REQUIRED_MESSAGE


def test_bigquery_table_is_partitioned_and_clustered(monkeypatch):
    if chat_turn_store.bigquery is None:
        pytest.skip("google-cloud-bigquery no instalado")

    created = {}

    class FakeClient:
        project = "project-test"

        def create_dataset(self, dataset, exists_ok=False):
            created["dataset"] = (dataset, exists_ok)

        def create_table(self, table, exists_ok=False):
            created["table"] = (table, exists_ok)

    chat_turn_store._ensure_table(FakeClient())
    table, exists_ok = created["table"]
    assert exists_ok is True
    assert table.time_partitioning.field == "fecha"
    assert table.clustering_fields == ["session_id", "orden"]


def test_order_is_continuous_and_session_cutoff_is_strict(local_store):
    first = chat_turn_store.append_turn(
        _turn(timestamp="2026-08-10T00:00:00+00:00")
    )
    exact_24h = chat_turn_store.append_turn(
        _turn(
            timestamp="2026-08-11T00:00:00+00:00",
            tipo="respuesta_usuario",
            autor="usuario",
            texto="Sí",
            seleccion_opcion=True,
            opcion_id_seleccionada="si",
        )
    )
    over_24h = chat_turn_store.append_turn(
        _turn(timestamp="2026-08-12T00:00:01+00:00")
    )

    assert [first["orden"], exact_24h["orden"], over_24h["orden"]] == [0, 1, 2]
    assert [first["nueva_sesion"], exact_24h["nueva_sesion"], over_24h["nueva_sesion"]] == [
        True,
        False,
        True,
    ]
    assert exact_24h["seleccion_opcion"] is True
    assert exact_24h["opcion_id_seleccionada"] == "si"


def test_options_are_stored_as_json_and_read_as_array(local_store):
    stored = chat_turn_store.append_turn(
        _turn(
            tipo="botones",
            opciones=[{"id": "a", "title": "A"}, {"id": "b", "title": "B", "description": "Desc"}],
            interactive_type="button",
        )
    )
    assert json.loads(stored["opciones"])[1]["description"] == "Desc"

    turns = chat_turn_store.read_turns()
    assert turns[0]["opciones"] == [
        {"id": "a", "title": "A"},
        {"id": "b", "title": "B", "description": "Desc"},
    ]


def test_local_read_filters_and_orders_globally(local_store):
    chat_turn_store.append_turn(_turn("wa:b", timestamp="2026-08-11T12:00:00+00:00"))
    chat_turn_store.append_turn(_turn("wa:a", timestamp="2026-08-10T12:00:00+00:00"))
    chat_turn_store.append_turn(_turn("wa:a", timestamp="2026-08-11T12:00:00+00:00"))

    all_turns = chat_turn_store.read_turns()
    assert [(turn["session_id"], turn["orden"]) for turn in all_turns] == [
        ("wa:a", 0),
        ("wa:a", 1),
        ("wa:b", 0),
    ]
    filtered = chat_turn_store.read_turns(
        session_id="wa:a",
        start=datetime(2026, 8, 11, tzinfo=timezone.utc).date(),
        end=datetime(2026, 8, 11, tzinfo=timezone.utc).date(),
    )
    assert [(turn["session_id"], turn["orden"]) for turn in filtered] == [("wa:a", 1)]


def test_cache_queries_bigquery_only_on_first_turn(monkeypatch):
    chat_turn_store.reset_runtime_state()
    calls = {"last": 0, "insert": 0}
    fake_client = object()
    monkeypatch.setattr(chat_turn_store, "_uses_bigquery", lambda: True)
    monkeypatch.setattr(chat_turn_store, "_get_client", lambda: fake_client)

    def fake_last(client, session_id):
        calls["last"] += 1
        return None

    def fake_insert(client, record):
        calls["insert"] += 1

    monkeypatch.setattr(chat_turn_store, "_last_turn_bigquery", fake_last)
    monkeypatch.setattr(chat_turn_store, "_append_bigquery", fake_insert)

    chat_turn_store.append_turn(_turn())
    chat_turn_store.append_turn(_turn(timestamp="2026-08-11T12:01:00+00:00"))

    assert calls == {"last": 1, "insert": 2}
    chat_turn_store.reset_runtime_state()


@pytest.mark.parametrize("failure_point", ["client", "hydrate"])
def test_initialize_never_propagates_creation_or_hydration_failures(monkeypatch, failure_point):
    chat_turn_store.reset_runtime_state()
    monkeypatch.setattr(chat_turn_store, "_uses_bigquery", lambda: True)
    if failure_point == "client":
        monkeypatch.setattr(
            chat_turn_store,
            "_get_client",
            lambda: (_ for _ in ()).throw(PermissionError("sin billing")),
        )
    else:
        monkeypatch.setattr(chat_turn_store, "_get_client", lambda: object())
        monkeypatch.setattr(
            chat_turn_store,
            "_hydrate_bigquery_cache",
            lambda _client: (_ for _ in ()).throw(PermissionError("sin permisos")),
        )

    assert chat_turn_store.initialize() is False
    chat_turn_store.reset_runtime_state()


def test_lookup_failure_does_not_insert_with_invented_order(monkeypatch):
    chat_turn_store.reset_runtime_state()
    inserted = []
    fake_client = object()
    monkeypatch.setattr(chat_turn_store, "_uses_bigquery", lambda: True)
    monkeypatch.setattr(chat_turn_store, "_get_client", lambda: fake_client)
    monkeypatch.setattr(
        chat_turn_store,
        "_last_turn_bigquery",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("BigQuery caído")),
    )
    monkeypatch.setattr(chat_turn_store, "_append_bigquery", lambda *_args: inserted.append(True))

    assert chat_turn_store.append_turn(_turn()) is None
    assert inserted == []
    assert "api:test" not in chat_turn_store._LAST_TURN_CACHE
    chat_turn_store.reset_runtime_state()


def test_write_failure_is_swallowed_and_does_not_advance_cache(monkeypatch):
    chat_turn_store.reset_runtime_state()
    fake_client = object()
    monkeypatch.setattr(chat_turn_store, "_uses_bigquery", lambda: True)
    monkeypatch.setattr(chat_turn_store, "_get_client", lambda: fake_client)
    monkeypatch.setattr(chat_turn_store, "_last_turn_bigquery", lambda *_args: None)
    monkeypatch.setattr(
        chat_turn_store,
        "_append_bigquery",
        lambda *_args: (_ for _ in ()).throw(PermissionError("billing disabled")),
    )

    assert chat_turn_store.append_turn(_turn()) is None
    assert "api:test" not in chat_turn_store._LAST_TURN_CACHE
    chat_turn_store.reset_runtime_state()
