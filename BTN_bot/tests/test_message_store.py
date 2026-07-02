"""
Tests de message_store.py.

Ejecutar:
    pytest tests/test_message_store.py
"""

import pytest

import message_store


@pytest.fixture(autouse=True)
def _clear_cache():
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
            "button_text": 2000,
        }
