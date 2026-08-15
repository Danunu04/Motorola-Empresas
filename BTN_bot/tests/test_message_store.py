"""
Tests de message_store.py.

Ejecutar:
    pytest tests/test_message_store.py
"""

import pytest
import copy
import json
from collections import defaultdict
from types import SimpleNamespace

import message_store


class FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class TombstoneHistoryClient:
    project = "test-project"

    def __init__(self):
        self.inserts = []
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        if "SELECT message_key, deleted" in sql:
            return FakeQueryJob([
                {"message_key": "welcome_message", "deleted": True},
            ])
        if "SELECT message_key, message_type" in sql:
            return FakeQueryJob([])
        raise AssertionError(f"query inesperada: {sql}")

    def insert_rows_json(self, table_id, rows, row_ids=None):
        self.inserts.extend(rows)
        return []


class StatefulMessageClient:
    project = "test-project"

    def __init__(self, initial_rows):
        self.history = copy.deepcopy(initial_rows)

    def _latest_rows(self):
        latest = {}
        for row in self.history:
            latest[row["message_key"]] = row
        return latest

    def query(self, sql):
        latest = self._latest_rows()
        if "SELECT message_key, deleted" in sql:
            return FakeQueryJob([
                {
                    "message_key": row["message_key"],
                    "deleted": row.get("deleted", False),
                }
                for row in latest.values()
            ])
        if "SELECT message_key, message_type" in sql:
            return FakeQueryJob([
                {
                    **copy.deepcopy(row),
                    "updated_at": None,
                }
                for row in latest.values()
                if not row.get("deleted", False)
            ])
        raise AssertionError(f"query inesperada: {sql}")

    def insert_rows_json(self, table_id, rows, row_ids=None):
        self.history.extend(copy.deepcopy(rows))
        return []


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr(message_store, "BIGQUERY_DATASET", "unit_test_dataset")
    message_store.invalidate_cache()
    yield
    message_store.invalidate_cache()


class TestGetMessage:
    def test_returns_seeded_default_content_for_known_key(self):
        content = message_store.get_message("welcome_message")
        assert "Plataforma de Beneficios de Motorola" in content

    def test_returns_caller_default_for_unknown_key(self):
        assert message_store.get_message("no_existe", default="fallback") == "fallback"

    def test_returns_empty_string_default_when_not_provided(self):
        assert message_store.get_message("no_existe") == ""


class TestLoadAllMessages:
    def test_returns_flat_dict_of_known_keys(self):
        messages = message_store.load_all_messages()
        assert isinstance(messages, dict)
        assert messages["welcome_message"]
        assert messages["main_menu_button_text"] == "Ver opciones"


class TestGetAllMessagesWithMetadata:
    def test_sorted_with_global_constants_first_then_by_state_and_order(self):
        items = message_store.get_all_messages_with_metadata()
        state_names = [item["state_name"] for item in items]
        first_non_null = next(i for i, s in enumerate(state_names) if s is not None)
        assert all(s is None for s in state_names[:first_non_null])

        # Within each state, orden should be non-decreasing.
        by_state: dict = {}
        for item in items:
            by_state.setdefault(item["state_name"], []).append(item)
        for state, state_items in by_state.items():
            ordens = [item.get("orden") for item in state_items]
            assert all(o is not None for o in ordens)
            assert ordens == sorted(ordens)

    def test_every_entry_tagged_default_without_bigquery(self):
        items = message_store.get_all_messages_with_metadata()
        assert items
        assert all(item["source"] == "default" for item in items)


class TestCatalogSourceComposition:
    def test_all_requested_catalogs_must_be_bigquery(self, monkeypatch):
        monkeypatch.setattr(message_store, "get_catalog_sources", lambda: {
            "messages": "bigquery",
            "bindings": "bigquery",
            "config": "bigquery",
        })

        assert message_store.compose_catalog_source(
            "messages", "bindings", "config"
        ) == "bigquery"

    def test_any_default_catalog_makes_composed_source_default(self, monkeypatch):
        monkeypatch.setattr(message_store, "get_catalog_sources", lambda: {
            "messages": "bigquery",
            "bindings": "default",
            "config": "bigquery",
        })

        assert message_store.compose_catalog_source(
            "messages", "bindings", "config"
        ) == "default"


class TestPhase1Catalog:
    def test_catalog_counts_private_copy_arithmetic_and_tombstones(self):
        assert len(message_store.DEFAULT_MESSAGES) == 177
        assert len([
            message for message in message_store.DEFAULT_MESSAGES
            if not message.get("deleted", False)
        ]) == 165
        source_counts = {
            source_key: sum(
                mapped_source == source_key
                for mapped_source in message_store.PHASE1_PRIVATE_MESSAGE_SOURCE_KEYS.values()
            )
            for source_key in set(message_store.PHASE1_PRIVATE_MESSAGE_SOURCE_KEYS.values())
        }
        assert sum(
            source_counts.get(source_key, 0)
            for source_key in message_store.OBSOLETE_GENERIC_MESSAGE_KEYS
        ) == 86
        assert source_counts["generic_listo_button"] == 2
        assert source_counts["ask_when_loaded_text"] == 2
        assert source_counts["pasos_step3_text"] == 2
        assert source_counts["pre_flujo_message"] == 4
        assert source_counts["preflujo_continuar_button"] == 4
        assert len(message_store.PHASE1_PRIVATE_MESSAGE_SOURCE_KEYS) == 100
        assert all(
            next(
                message for message in message_store.DEFAULT_MESSAGES
                if message["message_key"] == key
            )["deleted"]
            for key in message_store.OBSOLETE_MESSAGE_KEYS
        )
        assert message_store.get_message_metadata("generic_yes_button") is None
        assert message_store.get_message_metadata("pre_flujo_message") is None
        assert message_store.get_message_metadata("preflujo_continuar_button") is None

    def test_26_group_configs_have_unique_prompts_and_declared_shared_prev_keys(self):
        assert len(message_store.DEFAULT_OPTION_GROUP_CONFIGS) == 26
        prompts = [config["prompt_key"] for config in message_store.DEFAULT_OPTION_GROUP_CONFIGS]
        assert len(prompts) == len(set(prompts))

        discount = message_store.get_option_group_config(
            "pasos_resultado_desde_descuentos"
        )
        form = message_store.get_option_group_config("pasos_resultado_desde_formulario")
        after_how = message_store.get_option_group_config(
            "borrar_nav_esperando_confirmacion_tras_explicar_como"
        )
        configs_with_prev = {
            config["option_group"]
            for config in message_store.DEFAULT_OPTION_GROUP_CONFIGS
            if config["prev_message_keys"] != "[]"
        }
        assert configs_with_prev == {
            "pasos_resultado_desde_descuentos",
            "pasos_resultado_desde_formulario",
            "borrar_nav_esperando_confirmacion_tras_explicar_como",
        }
        assert discount["prev_message_keys"] == [
            "descuentos_login_steps_text", "pasos_step1_text", "pasos_step2_text"
        ]
        assert form["prev_message_keys"] == [
            "formulario_login_steps_text", "pasos_step1_text", "pasos_step2_text"
        ]
        assert discount["shared_prev_keys"] == ["pasos_step1_text", "pasos_step2_text"]
        assert form["shared_prev_keys"] == ["pasos_step1_text", "pasos_step2_text"]
        assert after_how["prev_message_keys"] == ["borrar_nav_how_to_text"]
        assert after_how["shared_prev_keys"] == []

    def test_group_owned_messages_are_exclusive_except_two_marked_prev_keys(self):
        direct_owners = defaultdict(set)
        for option in message_store.DEFAULT_OPTION_BINDINGS:
            for field in ("title_key", "button_title_key", "description_key"):
                if option.get(field):
                    direct_owners[option[field]].add(option["option_group"])
        for config in message_store.DEFAULT_OPTION_GROUP_CONFIGS:
            for field in ("button_text_key", "section_title_key", "prompt_key"):
                direct_owners[config[field]].add(config["option_group"])
        assert {
            key: owners for key, owners in direct_owners.items() if len(owners) > 1
        } == {}

        prev_owners = defaultdict(set)
        for config in message_store.DEFAULT_OPTION_GROUP_CONFIGS:
            for key in json.loads(config["prev_message_keys"]):
                prev_owners[key].add(config["option_group"])
        assert {
            key: owners for key, owners in prev_owners.items() if len(owners) > 1
        } == {
            "pasos_step1_text": {
                "pasos_resultado_desde_descuentos", "pasos_resultado_desde_formulario"
            },
            "pasos_step2_text": {
                "pasos_resultado_desde_descuentos", "pasos_resultado_desde_formulario"
            },
        }

    def test_phase1_migration_preserves_current_source_edits_in_all_private_copies(self):
        default_by_key = {
            message["message_key"]: message
            for message in message_store.DEFAULT_MESSAGES
        }
        source_keys = (
            set(message_store.PHASE1_PRIVATE_MESSAGE_SOURCE_KEYS.values())
            | set(message_store.OBSOLETE_MESSAGE_KEYS)
        )
        source_rows = []
        for source_key in source_keys:
            meta = default_by_key[source_key]
            source_rows.append({
                "message_key": source_key,
                "message_type": meta["message_type"],
                "state_name": meta["state_name"],
                "flujo_identificacion_mensaje": meta.get("flujo_identificacion_mensaje"),
                "label": meta["label"],
                "content": (
                    "✅ Sí editado antes de Fase 1"
                    if source_key == "generic_yes_button"
                    else meta["default_content"]
                ),
                "default_content": meta["default_content"],
                "orden": meta.get("orden"),
                "deleted": False,
                "updated_at": None,
                "updated_by": "admin",
            })

        class MigrationClient:
            project = "test-project"

            def __init__(self):
                self.inserts = []

            def query(self, sql):
                return FakeQueryJob(copy.deepcopy(source_rows))

            def insert_rows_json(self, table_id, rows, row_ids=None):
                self.inserts.append(copy.deepcopy(rows))
                return []

        client = MigrationClient()
        message_store._migrate_phase1_messages(client)

        copies, tombstones = client.inserts
        assert len(copies) == 100
        assert len(tombstones) == 12
        yes_copy_keys = {
            new_key
            for new_key, source_key in message_store.PHASE1_PRIVATE_MESSAGE_SOURCE_KEYS.items()
            if source_key == "generic_yes_button"
        }
        assert len(yes_copy_keys) == 9
        assert {
            row["message_key"] for row in copies
            if row["content"] == "✅ Sí editado antes de Fase 1"
        } == yes_copy_keys
        assert {row["message_key"] for row in tombstones} == set(
            message_store.OBSOLETE_MESSAGE_KEYS
        )
        assert all(row["deleted"] is True for row in tombstones)


class TestInvalidateCache:
    def test_clears_cache_and_allows_lazy_reload(self):
        message_store.get_message("welcome_message")
        message_store.invalidate_cache()
        # Lazy reload on next access still resolves the known key.
        assert message_store.get_message("welcome_message")


class TestSetMessage:
    def test_raises_runtime_error_without_bigquery_configured(self):
        with pytest.raises(RuntimeError):
            message_store.set_message("welcome_message", "Nuevo contenido", updated_by="qa")

    def test_raises_key_error_for_unknown_key(self):
        with pytest.raises(KeyError):
            message_store.set_message("no_existe", "contenido", updated_by="qa")


class TestBigQueryDatasetIsolation:
    def test_table_id_rejects_missing_explicit_dataset(self, monkeypatch):
        monkeypatch.setattr(message_store, "BIGQUERY_DATASET", "")
        client = SimpleNamespace(project="consultora-485520")

        with pytest.raises(RuntimeError) as exc_info:
            message_store._table_id(client)

        assert str(exc_info.value) == message_store.BIGQUERY_DATASET_REQUIRED_MESSAGE

    def test_legacy_table_is_scoped_to_explicit_dataset(self, monkeypatch):
        monkeypatch.setattr(message_store, "BIGQUERY_DATASET", "inspectia_logs_test")
        client = SimpleNamespace(project="consultora-485520")

        assert message_store._table_id(
            client, message_store.LEGACY_BOT_MESSAGES_TABLE
        ) == "consultora-485520.inspectia_logs_test.bot_messages"


class TestCreateMessage:
    def test_raises_value_error_for_existing_key(self):
        with pytest.raises(ValueError):
            message_store.create_message(
                message_key="welcome_message",
                message_type="text",
                state_name=None,
                label="x",
                content="x",
                updated_by="qa",
            )

    def test_raises_value_error_for_invalid_type(self):
        with pytest.raises(ValueError):
            message_store.create_message(
                message_key="nuevo",
                message_type="video",
                state_name=None,
                label="x",
                content="x",
                updated_by="qa",
            )

    def test_raises_value_error_for_over_limit_content(self):
        with pytest.raises(ValueError):
            message_store.create_message(
                message_key="nuevo",
                message_type="list_row_title",
                state_name=None,
                label="x",
                content="x" * 25,
                updated_by="qa",
            )

    def test_raises_runtime_error_without_bigquery(self):
        with pytest.raises(RuntimeError):
            message_store.create_message(
                message_key="nuevo_valido",
                message_type="text",
                state_name=None,
                label="x",
                content="x",
                updated_by="qa",
            )


class TestMessageLimits:
    def test_documented_limits(self):
        assert message_store.MESSAGE_LIMITS == {
            "button": 20,
            "list_row_title": 24,
            "list_row_description": 72,
            "text": 2000,
            "button_text": 20,
            "list_section_title": 24,
        }


class TestMessageLogicalDelete:
    def test_messages_schema_includes_deleted_with_false_default(self):
        if message_store.bigquery is None:
            pytest.skip("google-cloud-bigquery no instalado")

        fields = message_store._messages_schema()
        deleted = next(field for field in fields if field.name == "deleted")

        assert deleted.field_type == "BOOLEAN"
        assert deleted.default_value_expression == "FALSE"

    def test_existing_table_migration_adds_deleted_once(self):
        if message_store.bigquery is None:
            pytest.skip("google-cloud-bigquery no instalado")

        table = SimpleNamespace(schema=[
            message_store.bigquery.SchemaField("message_key", "STRING", mode="REQUIRED"),
        ])

        class FakeSchemaClient:
            project = "test-project"

            def __init__(self):
                self.updated_fields = []

            def get_table(self, table_id):
                return table

            def update_table(self, updated_table, fields):
                self.updated_fields.append(tuple(fields))
                return updated_table

        client = FakeSchemaClient()

        message_store._ensure_messages_deleted_column(client)
        message_store._ensure_messages_deleted_column(client)

        deleted = next(field for field in table.schema if field.name == "deleted")
        assert deleted.field_type == "BOOLEAN"
        assert deleted.default_value_expression == "FALSE"
        assert client.updated_fields == [("schema",)]

    def test_loader_selects_latest_versions_and_filters_tombstones(self, monkeypatch):
        client = TombstoneHistoryClient()
        monkeypatch.setattr(message_store, "_get_client", lambda: client)

        assert message_store._load_from_bigquery() == []

        sql = client.queries[-1]
        assert "PARTITION BY message_key" in sql
        assert "WHERE deleted = FALSE OR deleted IS NULL" in sql

    def test_deleted_default_does_not_reappear_after_restart(self, monkeypatch):
        welcome = next(
            message
            for message in message_store.DEFAULT_MESSAGES
            if message["message_key"] == "welcome_message"
        )
        client = StatefulMessageClient([{
            "message_key": welcome["message_key"],
            "message_type": welcome["message_type"],
            "state_name": welcome["state_name"],
            "flujo_identificacion_mensaje": welcome["flujo_identificacion_mensaje"],
            "label": welcome["label"],
            "content": welcome["default_content"],
            "default_content": welcome["default_content"],
            "orden": welcome["orden"],
            "deleted": False,
            "updated_at": None,
            "updated_by": "system-seed",
        }])
        monkeypatch.setattr(message_store, "DEFAULT_MESSAGES", [welcome])
        monkeypatch.setattr(message_store, "_bigquery_configured", lambda: True)
        monkeypatch.setattr(message_store, "_get_client", lambda: client)
        message_store.invalidate_cache()

        message_store.delete_message("welcome_message", updated_by="qa")
        with pytest.raises(KeyError):
            message_store.delete_message("welcome_message", updated_by="qa")

        history_size = len(client.history)
        message_store._ensure_missing_message_defaults(client)
        assert len(client.history) == history_size

        message_store.invalidate_cache()
        assert message_store.get_message_metadata("welcome_message") is None
