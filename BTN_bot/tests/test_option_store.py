"""Tests permanentes del almacenamiento append-only de opciones."""

import copy
import json

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


class FakeVersionJob:
    def __init__(self, version_ids):
        self.version_ids = version_ids

    def result(self):
        return [{"version_id": version_id} for version_id in self.version_ids]


class FakeExistingSeedClient(FakeInsertClient):
    def __init__(self, option_versions, config_versions):
        super().__init__()
        self.option_versions = option_versions
        self.config_versions = config_versions

    def query(self, sql):
        versions = self.option_versions if "option_bindings" in sql else self.config_versions
        return FakeVersionJob(versions)


@pytest.fixture(autouse=True)
def _local_defaults(monkeypatch):
    monkeypatch.setattr(message_store, "BIGQUERY_DATASET", "unit_test_dataset")
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
    def test_option_bindings_schema_has_phase1_18_fields(self):
        if message_store.bigquery is None:
            pytest.skip("google-cloud-bigquery no instalado")
        fields = message_store._option_bindings_schema()
        assert [field.name for field in fields] == [
            "option_group", "option_id", "orden", "title_key", "button_title_key",
            "description_key", "target_state", "target_vars", "target_option_group", "stay_in_state",
            "target_substep_key", "target_substep_value", "reply_key", "extra_flags",
            "deleted", "updated_at", "version_id", "updated_by",
        ]
        assert next(field for field in fields if field.name == "deleted").default_value_expression == "FALSE"

    def test_option_group_config_schema_has_phase1_10_fields(self):
        if message_store.bigquery is None:
            pytest.skip("google-cloud-bigquery no instalado")
        fields = message_store._option_group_config_schema()
        assert [field.name for field in fields] == [
            "option_group", "button_text_key", "section_title_key", "prompt_key",
            "prev_message_keys", "shared_prev_keys", "deleted",
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

    def test_preflow_bindings_have_no_legacy_router_variables(self):
        menu_options = [
            option for option in message_store.DEFAULT_OPTION_BINDINGS
            if option["option_group"] == "menu_principal"
        ]
        assert all(json.loads(option["target_vars"]) == {"intentos_identificacion": 0}
                   for option in menu_options)
        discount_continue = next(
            option for option in message_store.DEFAULT_OPTION_BINDINGS
            if option["option_group"] == "preflujo_desde_descuentos"
        )
        assert json.loads(discount_continue["target_vars"]) == {
            "flujo": "descuentos",
            "descuentos_substep": "pregunta_cargaste",
        }

    def test_unknown_group_receives_generic_config(self):
        config = message_store.get_option_group_config("grupo_nuevo")
        assert config["button_text"] == "Ver opciones"
        assert config["section_title"] == "Opciones"
        assert config["button_text_key"] == "grupo_nuevo_button_text"
        assert config["prompt_key"] == "grupo_nuevo_prompt_text"
        assert config["prev_message_keys"] == []
        assert config["shared_prev_keys"] == []
        assert config["source"] == "default-fallback"

    def test_seed_snapshot_has_47_active_options_and_26_active_configs(self):
        client = FakeQueryClient()

        message_store._seed_option_tables(client)

        assert len(client.inserts) == 2
        option_rows = client.inserts[0]["rows"]
        config_rows = client.inserts[1]["rows"]
        assert len([row for row in option_rows if not row["deleted"]]) == 47
        assert len([row for row in option_rows if row["deleted"]]) == 7
        assert len([row for row in config_rows if not row["deleted"]]) == 26
        assert len([row for row in config_rows if row["deleted"]]) == 4
        assert all("target_option_group" in row for row in option_rows)
        assert all(row.get("prompt_key") for row in config_rows)

    def test_previous_seed_only_receives_four_menu_updates_and_four_new_preflow_rows(self):
        option_versions = {
            message_store._deterministic_seed_id(
                message_store.BIGQUERY_OPTION_BINDINGS_TABLE,
                f'{option["option_group"]}:{option["option_id"]}',
            )
            for option in message_store.DEFAULT_OPTION_BINDINGS
            if option["option_group"] not in message_store.PREFLOW_GROUPS
        }
        option_versions.update({
            message_store._deterministic_seed_id(
                message_store.BIGQUERY_OPTION_BINDINGS_TABLE,
                f'obsolete:{option["option_group"]}:{option["option_id"]}',
            )
            for option in message_store.LEGACY_PHASE1_OPTION_BINDINGS
        })
        config_versions = {
            message_store._deterministic_seed_id(
                message_store.BIGQUERY_OPTION_GROUP_CONFIG_TABLE,
                config["option_group"],
            )
            for config in message_store.DEFAULT_OPTION_GROUP_CONFIGS
            if config["option_group"] not in message_store.PREFLOW_GROUPS
        }
        config_versions.update({
            message_store._deterministic_seed_id(
                message_store.BIGQUERY_OPTION_GROUP_CONFIG_TABLE,
                f"obsolete:{option_group}",
            )
            for option_group in message_store.LEGACY_PHASE1_OPTION_GROUPS
        })
        client = FakeExistingSeedClient(option_versions, config_versions)

        message_store._seed_option_tables(client)

        option_rows = client.inserts[0]["rows"]
        config_rows = client.inserts[1]["rows"]
        assert len(option_rows) == 8
        assert {
            (row["option_group"], row["option_id"])
            for row in option_rows
        } == {
            *(('menu_principal', option_id)
              for option_id in message_store.PREFLOW_ENTRY_GROUP_BY_MENU_OPTION),
            *((group, 'continuar') for group in message_store.PREFLOW_GROUPS),
        }
        assert {row["option_group"] for row in config_rows} == set(
            message_store.PREFLOW_GROUPS
        )


class TestAppendOnlyWrites:
    def test_create_inserts_new_version_without_update_or_delete(self, monkeypatch):
        client = FakeInsertClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        monkeypatch.setattr(message_store, "get_option_set", lambda group: [])
        monkeypatch.setattr(message_store, "_ensure_persisted_group_config", lambda *args: None)

        row = message_store.create_option_binding(
            "grupo", "nueva", 10, "info_pedido_opciones_si_title",
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
            "grupo_nuevo", "primera", 10, "info_pedido_opciones_si_title",
            target_state="EstadoInicial", updated_by="qa",
        )

        assert [insert["table_id"].split(".")[-1] for insert in client.inserts] == [
            "option_group_config", "option_bindings",
        ]
        config = client.inserts[0]["rows"][0]
        assert config["button_text_key"] == "grupo_nuevo_button_text"
        assert config["section_title_key"] == "grupo_nuevo_section_title"
        assert config["prompt_key"] == "grupo_nuevo_prompt_text"

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
            "grupo", "main_menu_button_text", "main_menu_section_title", "welcome_message",
            updated_by="qa",
        )

        assert len(client.inserts) == 1
        assert client.inserts[0]["table_id"].endswith(".option_group_config")
        assert row["deleted"] is False
        assert row["version_id"]

    def test_group_config_persists_ordered_prev_and_shared_keys(self, monkeypatch):
        client = FakeInsertClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)

        row = message_store.set_option_group_config(
            "grupo",
            "main_menu_button_text",
            "main_menu_section_title",
            "welcome_message",
            ["pasos_step1_text", "pasos_step2_text"],
            ["pasos_step2_text"],
            updated_by="qa",
        )

        assert row["prev_message_keys"] == '["pasos_step1_text","pasos_step2_text"]'
        assert row["shared_prev_keys"] == '["pasos_step2_text"]'

    def test_group_config_rejects_shared_key_outside_prev(self):
        with pytest.raises(ValueError, match="subconjunto"):
            message_store.set_option_group_config(
                "grupo",
                "main_menu_button_text",
                "main_menu_section_title",
                "welcome_message",
                ["pasos_step1_text"],
                ["pasos_step2_text"],
            )


class TestOptionConstraints:
    def test_stay_in_state_allows_removing_its_additional_reply(self):
        row = _seed_option()
        row.update({
            "stay_in_state": True,
            "target_state": None,
            "target_substep_key": "paso",
            "target_substep_value": "fin",
            "target_option_group": None,
            "reply_key": None,
        })

        normalized = message_store._normalize_option_record(row)

        assert normalized["stay_in_state"] is True
        assert normalized["reply_key"] is None
        assert normalized["target_option_group"] is None

    def test_create_rejects_eleventh_active_option(self, monkeypatch):
        monkeypatch.setattr(
            message_store,
            "get_option_set",
            lambda group: [_seed_option(option_id=str(index)) for index in range(10)],
        )
        with pytest.raises(ValueError, match="superar 10"):
            message_store.create_option_binding(
                "grupo", "once", 110, "info_pedido_opciones_si_title", target_state="EstadoInicial"
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
                "grupo", "test_button_text", "main_menu_section_title", "welcome_message"
            )
        with pytest.raises(ValueError, match="section_title_key excede 24"):
            message_store.set_option_group_config(
                "grupo", "main_menu_button_text", "test_section", "welcome_message"
            )
