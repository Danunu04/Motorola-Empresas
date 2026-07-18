"""Tests permanentes del almacenamiento append-only de opciones."""

import copy

import pytest

import message_store


class FakeInsertClient:
    project = "test-project"

    def __init__(self):
        self.inserts = []

    def insert_rows_json(self, table_id, rows, row_ids=None):
        self.inserts.append({
            "table_id": table_id,
            "rows": copy.deepcopy(rows),
            "row_ids": copy.deepcopy(row_ids),
        })
        return []


class FakeQueryJob:
    def result(self):
        return []


class FakeQueryClient(FakeInsertClient):
    def __init__(self):
        super().__init__()
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return FakeQueryJob()


@pytest.fixture(autouse=True)
def _local_defaults(monkeypatch):
    monkeypatch.setattr(message_store, "_bigquery_configured", lambda: False)
    message_store.invalidate_cache()
    yield
    message_store.invalidate_cache()


def _seed_option(option_group="grupo", option_id="uno"):
    row = copy.deepcopy(message_store.DEFAULT_OPTION_BINDINGS[4])
    row["option_group"] = option_group
    row["option_id"] = option_id
    return row


class TestOptionSchemas:
    def test_option_bindings_schema_has_final_17_fields(self):
        if message_store.bigquery is None:
            pytest.skip("google-cloud-bigquery no instalado")
        fields = message_store._option_bindings_schema()
        assert [field.name for field in fields] == [
            "option_group", "option_id", "orden", "title_key", "button_title_key",
            "description_key", "target_state", "target_vars", "stay_in_state",
            "target_substep_key", "target_substep_value", "reply_key", "extra_flags",
            "deleted", "updated_at", "version_id", "updated_by",
        ]
        assert next(field for field in fields if field.name == "deleted").default_value_expression == "FALSE"

    def test_option_group_config_schema_has_final_7_fields(self):
        if message_store.bigquery is None:
            pytest.skip("google-cloud-bigquery no instalado")
        fields = message_store._option_group_config_schema()
        assert [field.name for field in fields] == [
            "option_group", "button_text_key", "section_title_key", "deleted",
            "updated_at", "version_id", "updated_by",
        ]

    def test_latest_queries_partition_by_logical_keys_and_filter_tombstones(self, monkeypatch):
        client = FakeQueryClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)

        assert message_store._load_options_from_bigquery() == []
        assert message_store._load_option_group_configs_from_bigquery() == []

        option_sql, config_sql = client.queries
        assert "PARTITION BY option_group, option_id" in option_sql
        assert "ORDER BY updated_at DESC, version_id DESC" in option_sql
        assert "WHERE deleted = FALSE OR deleted IS NULL" in option_sql
        assert "PARTITION BY option_group" in config_sql
        assert "ORDER BY updated_at DESC, version_id DESC" in config_sql
        assert "WHERE deleted = FALSE OR deleted IS NULL" in config_sql


class TestOptionFallbacks:
    def test_defaults_load_without_bigquery(self):
        options = message_store.get_option_set("menu_principal")
        assert len(options) == 4
        registro = next(option for option in options if option["option_id"] == "registro")
        assert registro["title"] == "No me puedo registrar"
        assert registro["button_title"] == "No puedo registrarme"

    def test_unknown_group_receives_generic_config(self):
        config = message_store.get_option_group_config("grupo_nuevo")
        assert config["button_text"] == "Ver opciones"
        assert config["section_title"] == "Opciones"
        assert config["source"] == "default-fallback"


class TestAppendOnlyWrites:
    def test_create_inserts_new_version_without_update_or_delete(self, monkeypatch):
        client = FakeInsertClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        monkeypatch.setattr(message_store, "get_option_set", lambda group: [])
        monkeypatch.setattr(message_store, "_ensure_persisted_group_config", lambda *args: None)

        row = message_store.create_option_binding(
            "grupo", "nueva", 10, "generic_yes_button",
            target_state="EstadoInicial", updated_by="qa",
        )

        assert len(client.inserts) == 1
        assert client.inserts[0]["table_id"].endswith(".option_bindings")
        assert row["deleted"] is False
        assert row["version_id"]
        assert row["updated_by"] == "qa"

    def test_first_option_also_inserts_default_group_config(self, monkeypatch):
        client = FakeInsertClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        monkeypatch.setattr(message_store, "get_option_set", lambda group: [])
        monkeypatch.setattr(message_store, "_ensure_option_cache_fresh", lambda: None)
        with message_store._OPTION_CACHE_LOCK:
            message_store._OPTION_CACHE.clear()
            message_store._OPTION_CONFIG_CACHE.clear()

        message_store.create_option_binding(
            "grupo_nuevo", "primera", 10, "generic_yes_button",
            target_state="EstadoInicial", updated_by="qa",
        )

        assert [insert["table_id"].split(".")[-1] for insert in client.inserts] == [
            "option_group_config", "option_bindings",
        ]
        config = client.inserts[0]["rows"][0]
        assert config["button_text_key"] == "generic_options_button_text"
        assert config["section_title_key"] == "generic_options_section_title"

    def test_update_inserts_complete_new_version(self, monkeypatch):
        client = FakeInsertClient()
        current = _seed_option()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        monkeypatch.setattr(message_store, "get_option_by_id", lambda *args: copy.deepcopy(current))

        row = message_store.update_option_binding(
            "grupo", "uno", orden=90, updated_by="editor",
        )

        assert len(client.inserts) == 1
        assert row["orden"] == 90
        assert row["title_key"] == current["title_key"]
        assert row["deleted"] is False
        assert row["updated_by"] == "editor"

    def test_delete_inserts_tombstone_with_complete_previous_values(self, monkeypatch):
        client = FakeInsertClient()
        current = _seed_option(option_id="uno")
        other = _seed_option(option_id="dos")
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        monkeypatch.setattr(
            message_store, "get_option_set", lambda group: [copy.deepcopy(current), copy.deepcopy(other)]
        )

        row = message_store.delete_option_binding("grupo", "uno", updated_by="editor")

        assert len(client.inserts) == 1
        assert row["deleted"] is True
        assert row["title_key"] == current["title_key"]
        assert row["option_id"] == "uno"

    def test_group_config_change_is_an_inserted_version(self, monkeypatch):
        client = FakeInsertClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)

        row = message_store.set_option_group_config(
            "grupo", "generic_options_button_text", "generic_options_section_title",
            updated_by="qa",
        )

        assert len(client.inserts) == 1
        assert client.inserts[0]["table_id"].endswith(".option_group_config")
        assert row["deleted"] is False
        assert row["version_id"]


class TestOptionConstraints:
    def test_create_rejects_eleventh_active_option(self, monkeypatch):
        monkeypatch.setattr(
            message_store,
            "get_option_set",
            lambda group: [_seed_option(option_id=str(index)) for index in range(10)],
        )
        with pytest.raises(ValueError, match="superar 10"):
            message_store.create_option_binding(
                "grupo", "once", 110, "generic_yes_button", target_state="EstadoInicial"
            )

    def test_delete_rejects_last_active_option_before_writing(self, monkeypatch):
        client = FakeInsertClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        monkeypatch.setattr(message_store, "get_option_set", lambda group: [_seed_option()])

        with pytest.raises(ValueError, match="ultima opcion"):
            message_store.delete_option_binding("grupo", "uno")
        assert client.inserts == []

    def test_invalid_json_object_is_rejected(self):
        with pytest.raises(ValueError, match="objeto JSON"):
            message_store._normalize_option_record({
                **_seed_option(),
                "target_vars": ["no", "es", "objeto"],
            })

    def test_description_and_group_config_lengths_are_validated(self, monkeypatch):
        original_metadata = message_store.get_message_metadata
        original_message = message_store.get_message

        monkeypatch.setattr(
            message_store,
            "get_message_metadata",
            lambda key: {"message_key": key} if key.startswith("test_") else original_metadata(key),
        )
        monkeypatch.setattr(
            message_store,
            "get_message",
            lambda key, default="": {
                "test_description": "x" * 73,
                "test_button_text": "x" * 21,
                "test_section": "x" * 25,
            }.get(key, original_message(key, default=default)),
        )

        with pytest.raises(ValueError, match="description_key excede 72"):
            message_store._normalize_option_record({
                **_seed_option(),
                "description_key": "test_description",
            })
        with pytest.raises(ValueError, match="button_text_key excede 20"):
            message_store.set_option_group_config(
                "grupo", "test_button_text", "generic_options_section_title"
            )
        with pytest.raises(ValueError, match="section_title_key excede 24"):
            message_store.set_option_group_config(
                "grupo", "generic_options_button_text", "test_section"
            )
