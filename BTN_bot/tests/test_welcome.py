"""
Tests de humo para el mensaje de bienvenida y los flujos iniciales del bot.

Ejecutar:
    pytest tests/test_welcome.py
"""

import re

import pytest

from flow_states_btn import (
    WELCOME_MESSAGE,
    FlowController,
    StateFactory,
)


EXPECTED_WELCOME = (
    "Hola, gracias por comunicarte con la Plataforma de Beneficios de Motorola.\n"
    "¿En qué te puedo ayudar hoy?"
)


def _build_controller() -> FlowController:
    # El flujo y los dominios se manejan internamente en flow_states_btn.py.
    return FlowController(flow_spec={}, company_domains={})


def _send(controller: FlowController, session_id: str, text: str):
    context = controller.get_or_create_session(session_id)
    return controller.process_message(context, text)


class TestWelcomeMessage:
    def test_welcome_message_matches_expected_text(self):
        assert WELCOME_MESSAGE == EXPECTED_WELCOME

    def test_welcome_message_has_no_blank_lines(self):
        assert "\n\n" not in WELCOME_MESSAGE
        assert WELCOME_MESSAGE.endswith("?")
        assert not WELCOME_MESSAGE.endswith("\n")

    def test_welcome_message_does_not_mention_hot_sale(self):
        lowered = WELCOME_MESSAGE.lower()
        forbidden = ["hot sale", "hotsale", "hot-sale", "hot_sale"]
        for token in forbidden:
            assert token not in lowered, f"WELCOME_MESSAGE contiene '{token}'"


class TestInitialGreeting:
    def test_hola_returns_welcome_message_with_list_menu(self):
        controller = _build_controller()
        result = _send(controller, "session-hola", "hola")

        assert result["reply"] == WELCOME_MESSAGE
        assert result["interactive_type"] == "list"
        assert result["buttons"] is None

        list_config = result.get("list_config")
        assert list_config is not None
        assert list_config["button_text"] == "Ver opciones"

        rows = list_config["sections"][0]["rows"]
        ids = {row["id"] for row in rows}
        assert ids == {"registro", "no_veo_precios", "no_veo_descuentos", "info_pedido"}

    def test_unrecognized_text_falls_back_to_welcome(self):
        controller = _build_controller()
        result = _send(controller, "session-fallback", "blablabla")

        # Hasta 3 intentos el bot reenvía la bienvenida.
        assert result["reply"] == WELCOME_MESSAGE
        assert result["interactive_type"] == "list"

    def test_after_three_fallbacks_it_offers_handoff(self):
        controller = _build_controller()
        session_id = "session-handoff"

        for _ in range(3):
            result = _send(controller, session_id, "xyz")

        assert "persona más calificada" in result["reply"].lower()
        assert result.get("handoff") is True


class TestWelcomeMessageIsReused:
    def test_all_initial_returns_use_welcome_message(self):
        """
        Recorre estados que vuelven al inicio y verifica que reutilicen
        WELCOME_MESSAGE en lugar de un string duplicado.
        """
        controller = _build_controller()

        # El botón de info_pedido pasa por EstadoPreFlujo, luego EstadoInfoPedido.
        controller.get_or_create_session("reused-info")
        _send(controller, "reused-info", "hola")
        _send(controller, "reused-info", "button_info_pedido")
        _send(controller, "reused-info", "button_continuar")
        result = _send(controller, "reused-info", "button_si")
        assert result["reply"] == WELCOME_MESSAGE


class TestMainFlowsSmoke:
    """Smoke tests para confirmar que los cuatro botones iniciales no rompen."""

    @pytest.mark.parametrize("button_id", [
        "registro",
        "no_veo_precios",
        "no_veo_descuentos",
        "info_pedido",
    ])
    def test_main_menu_buttons_start_flow(self, button_id: str):
        controller = _build_controller()
        session_id = f"session-{button_id}"

        _send(controller, session_id, "hola")
        result = _send(controller, session_id, f"button_{button_id}")

        assert result["reply"]
        assert result["reply"] != WELCOME_MESSAGE
        assert result.get("mode") == "flow"

    def test_registro_flow_handles_email(self):
        controller = _build_controller()
        sid = "session-registro-email"

        _send(controller, sid, "hola")
        _send(controller, sid, "button_registro")
        _send(controller, sid, "button_continuar")
        result = _send(controller, sid, "miusuario@gmail.com")

        assert result["reply"]
        assert "cargaste" in result["reply"].lower() or "formulario" in result["reply"].lower()

    def test_precios_flow_handles_email(self):
        controller = _build_controller()
        sid = "session-precios-email"

        _send(controller, sid, "hola")
        _send(controller, sid, "button_no_veo_precios")
        _send(controller, sid, "button_continuar")
        result = _send(controller, sid, "miusuario@gmail.com")

        assert result["reply"]
        assert "iniciaste" in result["reply"].lower() or "sesión" in result["reply"].lower()

    def test_descuentos_flow_asks_about_form(self):
        controller = _build_controller()
        sid = "session-descuentos"

        _send(controller, sid, "hola")
        _send(controller, sid, "button_no_veo_descuentos")
        result = _send(controller, sid, "button_continuar")

        assert "formulario" in result["reply"].lower()

    def test_info_pedido_flow_returns_whatsapp_link(self):
        controller = _build_controller()
        sid = "session-info"

        _send(controller, sid, "hola")
        _send(controller, sid, "button_info_pedido")
        result = _send(controller, sid, "button_continuar")

        assert "wa.me" in result["reply"]


class TestRegisteredStates:
    def test_all_expected_states_are_registered(self):
        expected = {
            "EstadoInicial",
            "EstadoPreFlujo",
            "EstadoPedirMail",
            "EstadoRegistroMailEmpresa",
            "EstadoLogin",
            "EstadoPasosInicioSesion",
            "EstadoConsultaAdicional",
            "EstadoFinalizado",
            "EstadoFormulario",
            "EstadoPortalBeneficios",
            "EstadoBorrarNavegacion",
            "EstadoSoporte",
            "EstadoNoVeoDescuentos",
            "EstadoInfoPedido",
        }
        registered = set(StateFactory.get_registered_states())
        assert expected <= registered, f"Faltan estados: {expected - registered}"
