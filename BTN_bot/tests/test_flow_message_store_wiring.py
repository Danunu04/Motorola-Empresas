"""
Tests del wiring entre flow_states_btn.py y message_store.py (HU futura, sección 9).

Verifican que los 11 mensajes editables se obtengan vía
message_store.get_message(key, default=CONST), con fallback a la constante
cuando el store no tiene valor.

Ejecutar:
    pytest tests/test_flow_message_store_wiring.py
"""

import pytest

import message_store
from flow_states_btn import (
    PRE_FLUJO_MESSAGE,
    WELCOME_MESSAGE,
    FlowController,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    message_store.invalidate_cache()
    yield
    message_store.invalidate_cache()


def _build_controller() -> FlowController:
    return FlowController(flow_spec={}, company_domains={})


def _send(controller: FlowController, session_id: str, text: str):
    context = controller.get_or_create_session(session_id)
    return controller.process_message(context, text)


EDITED_SUFFIX = " [EDITADO]"


def _patch_message(monkeypatch, key: str, edited_content: str) -> None:
    """Simula una edición guardada en el store para `key`, dejando el resto intacto."""
    original_get_message = message_store.get_message

    def fake_get_message(k, default=""):
        if k == key:
            return edited_content
        return original_get_message(k, default=default)

    monkeypatch.setattr(message_store, "get_message", fake_get_message)


class TestFallbackWithoutStoreValue:
    """Sin BigQuery configurado en tests, el store sirve los defaults -> debe igualar la constante."""

    def test_welcome_message_falls_back_to_constant(self):
        controller = _build_controller()
        result = _send(controller, "fallback-welcome", "hola")
        assert result["reply"] == WELCOME_MESSAGE

    def test_pre_flujo_message_falls_back_to_constant(self):
        controller = _build_controller()
        _send(controller, "fallback-preflujo", "hola")
        result = _send(controller, "fallback-preflujo", "button_registro")
        assert result["reply"] == PRE_FLUJO_MESSAGE

    def test_main_menu_button_text_falls_back_to_constant(self):
        controller = _build_controller()
        result = _send(controller, "fallback-button-text", "hola")
        assert result["list_config"]["button_text"] == "Ver opciones"

    def test_main_menu_rows_fall_back_to_constants(self):
        controller = _build_controller()
        result = _send(controller, "fallback-rows", "hola")
        rows = {row["id"]: row for row in result["list_config"]["sections"][0]["rows"]}
        assert rows["registro"]["title"] == "No me puedo registrar"
        assert rows["registro"]["description"] == "Problemas con el registro"
        assert rows["no_veo_precios"]["title"] == "No veo precios"
        assert rows["no_veo_descuentos"]["description"] == "Descuentos no aplicados"
        assert rows["info_pedido"]["title"] == "Info de mi pedido"


class TestEditedValueIsUsed:
    """Con un valor editado en el store, el bot debe devolver el contenido editado, no la constante."""

    def test_welcome_message_uses_edited_value(self, monkeypatch):
        edited = WELCOME_MESSAGE + EDITED_SUFFIX
        _patch_message(monkeypatch, "welcome_message", edited)

        controller = _build_controller()
        result = _send(controller, "edited-welcome", "hola")
        assert result["reply"] == edited

    def test_pre_flujo_message_uses_edited_value(self, monkeypatch):
        edited = PRE_FLUJO_MESSAGE + EDITED_SUFFIX
        _patch_message(monkeypatch, "pre_flujo_message", edited)

        controller = _build_controller()
        _send(controller, "edited-preflujo", "hola")
        result = _send(controller, "edited-preflujo", "button_no_veo_precios")
        assert result["reply"] == edited

    def test_main_menu_button_text_uses_edited_value(self, monkeypatch):
        _patch_message(monkeypatch, "main_menu_button_text", "Editado")

        controller = _build_controller()
        result = _send(controller, "edited-button-text", "hola")
        assert result["list_config"]["button_text"] == "Editado"

    def test_main_menu_row_title_uses_edited_value(self, monkeypatch):
        _patch_message(monkeypatch, "main_menu_row_registro_title", "Título editado")

        controller = _build_controller()
        result = _send(controller, "edited-row-title", "hola")
        rows = {row["id"]: row for row in result["list_config"]["sections"][0]["rows"]}
        assert rows["registro"]["title"] == "Título editado"

    def test_main_menu_row_description_uses_edited_value(self, monkeypatch):
        _patch_message(monkeypatch, "main_menu_row_info_pedido_description", "Descripción editada")

        controller = _build_controller()
        result = _send(controller, "edited-row-desc", "hola")
        rows = {row["id"]: row for row in result["list_config"]["sections"][0]["rows"]}
        assert rows["info_pedido"]["description"] == "Descripción editada"

    def test_welcome_message_edited_value_also_used_on_return_paths(self, monkeypatch):
        """Los retornos a EstadoInicial desde otros estados también deben usar el valor editado."""
        edited = WELCOME_MESSAGE + EDITED_SUFFIX
        _patch_message(monkeypatch, "welcome_message", edited)

        controller = _build_controller()
        sid = "edited-welcome-return"
        _send(controller, sid, "hola")
        _send(controller, sid, "button_info_pedido")
        _send(controller, sid, "button_continuar")
        result = _send(controller, sid, "button_si")
        assert result["reply"] == edited


    def test_generic_yes_button_uses_edited_value(self, monkeypatch):
        _patch_message(monkeypatch, "generic_yes_button", "Sí editado")

        controller = _build_controller()
        sid = "edited-yes-btn"
        _send(controller, sid, "hola")
        _send(controller, sid, "button_info_pedido")
        result = _send(controller, sid, "button_continuar")
        buttons = {btn["reply"]["id"]: btn["reply"]["title"] for btn in result["buttons"]}
        assert buttons["si"] == "Sí editado"

    def test_generic_si_loaded_button_uses_edited_value(self, monkeypatch):
        _patch_message(monkeypatch, "generic_si_loaded_button", "Sí cargué editado")

        controller = _build_controller()
        sid = "edited-si-loaded-btn"
        _send(controller, sid, "hola")
        _send(controller, sid, "button_no_veo_descuentos")
        result = _send(controller, sid, "button_continuar")
        buttons = {btn["reply"]["id"]: btn["reply"]["title"] for btn in result["buttons"]}
        assert buttons["si"] == "Sí cargué editado"

    def test_ask_loaded_form_text_uses_edited_value(self, monkeypatch):
        edited = "Contame si cargaste el formulario. [EDITADO]"
        _patch_message(monkeypatch, "ask_loaded_form_text", edited)

        controller = _build_controller()
        sid = "edited-ask-loaded"
        _send(controller, sid, "hola")
        _send(controller, sid, "button_no_veo_descuentos")
        _send(controller, sid, "button_continuar")
        result = _send(controller, sid, "tal vez")
        assert result["reply"] == edited

    def test_registro_intro_text_uses_edited_value(self, monkeypatch):
        edited = "Intro registro editada."
        _patch_message(monkeypatch, "registro_intro_text", edited)

        controller = _build_controller()
        sid = "edited-registro-intro"
        _send(controller, sid, "hola")
        _send(controller, sid, "button_registro")
        result = _send(controller, sid, "button_continuar")
        assert result["reply"] == edited


class TestConstantsPreservedAsFallback:
    """Las constantes originales no se eliminan: siguen siendo el default explícito."""

    def test_welcome_message_constant_unchanged(self):
        assert WELCOME_MESSAGE.startswith(
            "Hola, gracias por comunicarte con la Plataforma de Beneficios de Motorola."
        )

    def test_pre_flujo_message_constant_unchanged(self):
        assert PRE_FLUJO_MESSAGE.startswith("Entiendo que acceder a la Plataforma puede ser complicado")
