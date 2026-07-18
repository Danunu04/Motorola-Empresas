"""Tests permanentes del renderer y ruteo dinámico de option_groups."""

import json

import pytest

import message_store
from flow_states_btn import (
    FlowContext,
    OptionGroupRenderError,
    StateFactory,
    render_option_group,
)


GROUP_CONTEXTS = {
    "menu_principal": ("EstadoInicial", {}),
    "info_pedido_opciones": ("EstadoInfoPedido", {}),
    "registro_mail_empresa_opciones": ("EstadoRegistroMailEmpresa", {}),
    "login_opciones": ("EstadoLogin", {}),
    "pasos_inicio_sesion_resultado": ("EstadoPasosInicioSesion", {"login_step": 3}),
    "consulta_adicional_opciones": ("EstadoConsultaAdicional", {}),
    "finalizado_opciones": ("EstadoFinalizado", {}),
    "portal_beneficios_opciones": ("EstadoPortalBeneficios", {}),
    "descuentos_pregunta_cargaste": (
        "EstadoNoVeoDescuentos", {"descuentos_substep": "pregunta_cargaste"}
    ),
    "descuentos_pregunta_cuando": (
        "EstadoNoVeoDescuentos", {"descuentos_substep": "pregunta_cuando"}
    ),
    "formulario_pregunta_cargaste": (
        "EstadoFormulario", {"form_substep": "pregunta_cargaste"}
    ),
    "formulario_pregunta_cuando": (
        "EstadoFormulario", {"form_substep": "pregunta_cuando"}
    ),
    "borrar_nav_confirmar": (
        "EstadoBorrarNavegacion", {"clear_nav_step": "confirmar", "flujo": "registro"}
    ),
    "borrar_nav_explicar_motivo": (
        "EstadoBorrarNavegacion", {"clear_nav_step": "explicar_motivo", "flujo": "registro"}
    ),
    "borrar_nav_sabe_como": (
        "EstadoBorrarNavegacion", {"clear_nav_step": "sabe_como", "flujo": "registro"}
    ),
    "borrar_nav_esperando_confirmacion": (
        "EstadoBorrarNavegacion",
        {"clear_nav_step": "esperando_confirmacion", "flujo": "registro"},
    ),
    "borrar_nav_finalizado": (
        "EstadoBorrarNavegacion", {"clear_nav_step": "finalizado", "flujo": "registro"}
    ),
}


@pytest.fixture(autouse=True)
def _local_defaults(monkeypatch):
    monkeypatch.setattr(message_store, "_bigquery_configured", lambda: False)
    message_store.invalidate_cache()
    yield
    message_store.invalidate_cache()


def _make_state(option_group):
    state_name, variables = GROUP_CONTEXTS[option_group]
    context = FlowContext({}, {})
    context.update_vars(variables)
    context.update_session_data(code="MOTO123")
    state = StateFactory.create_state(state_name)
    assert state is not None
    context.set_state(state)
    return context, state


class TestGroupMappings:
    def test_all_17_seed_groups_are_mapped(self):
        seed_groups = {row["option_group"] for row in message_store.DEFAULT_OPTION_BINDINGS}
        assert seed_groups == set(GROUP_CONTEXTS)

        for option_group in seed_groups:
            context, state = _make_state(option_group)
            assert state.get_option_group(context) == option_group

    def test_agreed_groups_remain_without_dynamic_mapping(self):
        context = FlowContext({}, {})

        preflow = StateFactory.create_state("EstadoPreFlujo")
        context.set_state(preflow)
        assert preflow.get_option_group(context) is None

        support = StateFactory.create_state("EstadoSoporte")
        context.set_state(support)
        assert support.get_option_group(context) is None
        assert not hasattr(support, "prompt")

        discounts = StateFactory.create_state("EstadoNoVeoDescuentos")
        context.set_state(discounts)
        context.set_var("descuentos_substep", "esperando_resultado")
        assert discounts.get_option_group(context) is None
        assert len(discounts.get_buttons(context)) == 2


class TestAutomaticRenderer:
    def test_four_options_render_as_configured_list(self):
        rendered = render_option_group("menu_principal")
        rows = rendered.list_config["sections"][0]["rows"]

        assert rendered.interactive_type == "list"
        assert rendered.buttons == []
        assert rendered.list_config["button_text"] == "Ver opciones"
        assert rendered.list_config["sections"][0]["title"] == "¿En qué te puedo ayudar?"
        assert len(rows) == 4
        assert rows[0].title == "No me puedo registrar"

    def test_three_options_render_as_buttons_and_use_short_title(self, monkeypatch):
        original_get_option_set = message_store.get_option_set
        three_options = original_get_option_set("menu_principal")[:3]
        monkeypatch.setattr(
            message_store,
            "get_option_set",
            lambda group: three_options if group == "menu_principal" else original_get_option_set(group),
        )

        rendered = render_option_group("menu_principal")

        assert rendered.interactive_type == "button"
        assert rendered.list_config is None
        assert rendered.buttons[0].title == "No puedo registrarme"

    @pytest.mark.parametrize("count", [0, 11])
    def test_invalid_counts_raise_configuration_error(self, monkeypatch, count):
        monkeypatch.setattr(
            message_store,
            "get_option_set",
            lambda group: [
                {"option_id": str(index), "button_title": "B", "title": "T", "description": None}
                for index in range(count)
            ],
        )
        with pytest.raises(OptionGroupRenderError):
            render_option_group("invalido")


@pytest.mark.parametrize(
    "seed_option",
    message_store.DEFAULT_OPTION_BINDINGS,
    ids=lambda option: f"{option['option_group']}/{option['option_id']}",
)
def test_every_seed_option_routes_from_its_real_state(seed_option):
    option_group = seed_option["option_group"]
    context, source_state = _make_state(option_group)

    result = source_state.handle(context, f"button_{seed_option['option_id']}")

    target_vars = json.loads(seed_option["target_vars"]) if seed_option.get("target_vars") else {}
    for key, value in target_vars.items():
        assert context.get_var(key) == value

    if seed_option.get("stay_in_state"):
        assert context.get_state() is source_state
        assert context.get_var(seed_option["target_substep_key"]) == seed_option["target_substep_value"]
    else:
        expected_target = seed_option.get("target_state") or "EstadoSoporte"
        assert context.get_state().state_name == expected_target

    assert result["reply"]
    if (
        not seed_option.get("stay_in_state")
        and (seed_option.get("target_state") or "EstadoSoporte") == "EstadoSoporte"
    ):
        assert result["handoff"] is True


class TestRoutingFallbacksAndExceptions:
    def test_stale_option_routes_to_support(self, monkeypatch):
        context, state = _make_state("menu_principal")
        monkeypatch.setattr(message_store, "get_option_by_id", lambda *args: None)

        result = state.handle(context, "button_eliminada")

        assert context.get_state().state_name == "EstadoSoporte"
        assert result["handoff"] is True
        assert result["reply"]

    def test_legacy_invalid_target_routes_to_support(self, monkeypatch, caplog):
        context, state = _make_state("menu_principal")
        monkeypatch.setattr(
            message_store,
            "get_option_by_id",
            lambda *args: {
                "option_group": "menu_principal",
                "option_id": "legacy",
                "target_state": "EstadoLegacyInexistente",
                "target_vars": None,
                "stay_in_state": False,
                "reply_key": None,
                "extra_flags": None,
            },
        )

        result = state.handle(context, "button_legacy")

        assert context.get_state().state_name == "EstadoSoporte"
        assert result["handoff"] is True
        assert "target_state inválido" in caplog.text

    def test_preflujo_continuar_keeps_special_router(self):
        context = FlowContext({}, {})
        context.set_var("opcion_inicial", "registro")
        state = StateFactory.create_state("EstadoPreFlujo")
        context.set_state(state)

        state.handle(context, "button_continuar")

        assert context.get_state().state_name == "EstadoPedirMail"
        assert context.get_var("flujo") == "registro"

    def test_borrar_nav_listo_keeps_code_interpolation(self):
        context, state = _make_state("borrar_nav_esperando_confirmacion")

        result = state.handle(context, "button_listo")

        assert context.get_var("clear_nav_step") == "finalizado"
        assert "MOTO123" in result["reply"]

    @pytest.mark.parametrize(
        "state_name, message_key",
        [
            ("EstadoFinalizado", "goodbye_text"),
            ("EstadoConsultaAdicional", "follow_up_help_text"),
            ("EstadoFormulario", "formulario_question_text"),
            ("EstadoLogin", "ask_ingresado_antes_text"),
        ],
    )
    def test_destination_prompts_read_from_store(self, monkeypatch, state_name, message_key):
        context = FlowContext({}, {})
        state = StateFactory.create_state(state_name)
        context.set_state(state)
        original_get_message = message_store.get_message
        monkeypatch.setattr(
            message_store,
            "get_message",
            lambda key, default="": "EDITADO" if key == message_key else original_get_message(key, default),
        )

        assert state.prompt(context) == "EDITADO"
