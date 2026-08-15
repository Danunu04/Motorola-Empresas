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
    "preflujo_desde_registro": (
        "EstadoPreFlujo", {"target_option_group": "preflujo_desde_registro"}
    ),
    "preflujo_desde_precios": (
        "EstadoPreFlujo", {"target_option_group": "preflujo_desde_precios"}
    ),
    "preflujo_desde_descuentos": (
        "EstadoPreFlujo", {"target_option_group": "preflujo_desde_descuentos"}
    ),
    "preflujo_desde_info_pedido": (
        "EstadoPreFlujo", {"target_option_group": "preflujo_desde_info_pedido"}
    ),
    "info_pedido_opciones": ("EstadoInfoPedido", {}),
    "registro_mail_empresa_opciones": ("EstadoRegistroMailEmpresa", {}),
    "login_opciones": ("EstadoLogin", {}),
    "pasos_resultado_desde_descuentos": (
        "EstadoPasosInicioSesion",
        {"login_step": 3, "target_option_group": "pasos_resultado_desde_descuentos"},
    ),
    "pasos_resultado_desde_login": (
        "EstadoPasosInicioSesion",
        {"login_step": 3, "target_option_group": "pasos_resultado_desde_login"},
    ),
    "pasos_resultado_desde_formulario": (
        "EstadoPasosInicioSesion",
        {"login_step": 3, "target_option_group": "pasos_resultado_desde_formulario"},
    ),
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
    "borrar_nav_confirmar_desde_registro": (
        "EstadoBorrarNavegacion",
        {
            "clear_nav_step": "confirmar", "flujo": "registro",
            "target_option_group": "borrar_nav_confirmar_desde_registro",
        },
    ),
    "borrar_nav_confirmar_desde_portal": (
        "EstadoBorrarNavegacion",
        {
            "clear_nav_step": "confirmar", "flujo": "portal",
            "target_option_group": "borrar_nav_confirmar_desde_portal",
        },
    ),
    "borrar_nav_explicar_motivo": (
        "EstadoBorrarNavegacion", {"clear_nav_step": "explicar_motivo", "flujo": "registro"}
    ),
    "borrar_nav_sabe_como": (
        "EstadoBorrarNavegacion", {"clear_nav_step": "sabe_como", "flujo": "registro"}
    ),
    "borrar_nav_esperando_confirmacion_directa": (
        "EstadoBorrarNavegacion",
        {
            "clear_nav_step": "esperando_confirmacion", "flujo": "registro",
            "target_option_group": "borrar_nav_esperando_confirmacion_directa",
        },
    ),
    "borrar_nav_esperando_confirmacion_tras_explicar_como": (
        "EstadoBorrarNavegacion",
        {
            "clear_nav_step": "esperando_confirmacion", "flujo": "portal",
            "target_option_group": "borrar_nav_esperando_confirmacion_tras_explicar_como",
        },
    ),
    "borrar_nav_finalizado_desde_registro": (
        "EstadoBorrarNavegacion",
        {
            "clear_nav_step": "finalizado", "flujo": "registro",
            "target_option_group": "borrar_nav_finalizado_desde_registro",
        },
    ),
    "borrar_nav_finalizado_desde_portal": (
        "EstadoBorrarNavegacion",
        {
            "clear_nav_step": "finalizado", "flujo": "portal",
            "target_option_group": "borrar_nav_finalizado_desde_portal",
        },
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
    def test_all_26_seed_groups_are_mapped(self):
        seed_groups = {row["option_group"] for row in message_store.DEFAULT_OPTION_BINDINGS}
        assert seed_groups == set(GROUP_CONTEXTS)

        for option_group in seed_groups:
            context, state = _make_state(option_group)
            assert state.get_option_group(context) == option_group

    def test_only_states_without_option_groups_remain_on_legacy_rendering(self):
        context = FlowContext({}, {})

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


_MENU_OPTIONS = (
    ("registro", "No me puedo registrar", "Problemas con el registro"),
    ("no_veo_precios", "No veo precios", "No se muestran los precios"),
    ("no_veo_descuentos", "No veo descuentos", "Descuentos no aplicados"),
    ("info_pedido", "Info de mi pedido", "Consultar estado del pedido"),
)
_YES_NO_OPTIONS = (("si", "✅ Sí", None), ("no", "❌ No", None))
_RESULT_OPTIONS = (
    ("funciono", "✅ Funcionó", None), ("no_funciono", "❌ No funcionó", None)
)
_LOADED_OPTIONS = (
    ("si", "✅ Sí, lo cargué", None), ("no", "❌ No lo cargué", None)
)
_WHEN_OPTIONS = (
    ("menos_48", "Menos de 48hs", None), ("mas_48", "Más de 48hs", None)
)
_CONTINUE_OPTION = (("continuar", "CONTINUAR", None),)
_DONE_OPTION = (("listo", "✅ Ya lo hice", None),)
_BACK_OPTION = (("volver", "Volver al inicio", None),)


def _expect(reply_key, state_name, options=(), handoff=False):
    return reply_key, state_name, options, handoff


# Snapshot funcional previo a la separación de preflujo. Las filas privadas apuntan a la misma
# salida observable: así los 47 bindings quedan cubiertos sin derivar el esperado del seed nuevo.
PHASE0_EXPECTED_PATHS = {
    **{
        f"menu_principal/{option_id}": _expect(
            f"{preflow_group}_prompt_text", "EstadoPreFlujo", _CONTINUE_OPTION
        )
        for option_id, preflow_group in message_store.PREFLOW_ENTRY_GROUP_BY_MENU_OPTION.items()
    },
    "preflujo_desde_registro/continuar": _expect(
        "registro_intro_text", "EstadoPedirMail"
    ),
    "preflujo_desde_precios/continuar": _expect(
        "precios_intro_text", "EstadoPedirMail"
    ),
    "preflujo_desde_descuentos/continuar": _expect(
        "descuentos_intro_text", "EstadoNoVeoDescuentos", _LOADED_OPTIONS
    ),
    "preflujo_desde_info_pedido/continuar": _expect(
        "info_pedido_text", "EstadoInfoPedido", _YES_NO_OPTIONS
    ),
    "info_pedido_opciones/si": _expect("welcome_message", "EstadoInicial", _MENU_OPTIONS),
    "info_pedido_opciones/no": _expect("goodbye_text", "EstadoFinalizado", _BACK_OPTION),
    "registro_mail_empresa_opciones/si": _expect(
        "follow_up_help_text", "EstadoConsultaAdicional", _YES_NO_OPTIONS
    ),
    "registro_mail_empresa_opciones/no": _expect(
        "borrar_nav_confirm_text", "EstadoBorrarNavegacion", _YES_NO_OPTIONS
    ),
    "login_opciones/si": _expect(
        "login_steps_intro_text", "EstadoPasosInicioSesion", _RESULT_OPTIONS
    ),
    "login_opciones/no": _expect(
        "formulario_question_text", "EstadoFormulario", _LOADED_OPTIONS
    ),
    **{
        f"{group}/funciono": _expect(
            "follow_up_help_text", "EstadoConsultaAdicional", _YES_NO_OPTIONS
        )
        for group in (
            "pasos_resultado_desde_descuentos",
            "pasos_resultado_desde_login",
            "pasos_resultado_desde_formulario",
        )
    },
    **{
        f"{group}/no_funciono": _expect(
            "handoff_text", "EstadoSoporte", _BACK_OPTION, True
        )
        for group in (
            "pasos_resultado_desde_descuentos",
            "pasos_resultado_desde_login",
            "pasos_resultado_desde_formulario",
        )
    },
    "consulta_adicional_opciones/si": _expect(
        "welcome_message", "EstadoInicial", _MENU_OPTIONS
    ),
    "consulta_adicional_opciones/no": _expect(
        "goodbye_text", "EstadoFinalizado", _BACK_OPTION
    ),
    "finalizado_opciones/volver": _expect(
        "welcome_message", "EstadoInicial", _MENU_OPTIONS
    ),
    "portal_beneficios_opciones/si": _expect(
        "portal_beneficios_clear_nav_text", "EstadoBorrarNavegacion", _YES_NO_OPTIONS
    ),
    "portal_beneficios_opciones/no": _expect(
        "ask_ingresado_antes_text", "EstadoLogin", _YES_NO_OPTIONS
    ),
    "descuentos_pregunta_cargaste/si": _expect(
        "ask_when_loaded_text", "EstadoNoVeoDescuentos", _WHEN_OPTIONS
    ),
    "descuentos_pregunta_cargaste/no": _expect(
        "descuentos_not_loaded_text", "EstadoNoVeoDescuentos"
    ),
    "descuentos_pregunta_cuando/menos_48": _expect(
        "descuentos_wait_48_text", "EstadoNoVeoDescuentos"
    ),
    "descuentos_pregunta_cuando/mas_48": _expect(
        "descuentos_login_steps_text", "EstadoPasosInicioSesion"
    ),
    "formulario_pregunta_cargaste/si": _expect(
        "ask_when_loaded_text", "EstadoFormulario", _WHEN_OPTIONS
    ),
    "formulario_pregunta_cargaste/no": _expect(
        "not_loaded_form_text", "EstadoFormulario"
    ),
    "formulario_pregunta_cuando/menos_48": _expect(
        "wait_48_form_text", "EstadoFormulario"
    ),
    "formulario_pregunta_cuando/mas_48": _expect(
        "formulario_login_steps_text", "EstadoPasosInicioSesion"
    ),
    **{
        f"{group}/si": _expect(
            "borrar_nav_ask_know_how_text", "EstadoBorrarNavegacion", _YES_NO_OPTIONS
        )
        for group in (
            "borrar_nav_confirmar_desde_registro",
            "borrar_nav_confirmar_desde_portal",
        )
    },
    **{
        f"{group}/no": _expect(
            "borrar_nav_explain_why_text", "EstadoBorrarNavegacion", _YES_NO_OPTIONS
        )
        for group in (
            "borrar_nav_confirmar_desde_registro",
            "borrar_nav_confirmar_desde_portal",
        )
    },
    "borrar_nav_explicar_motivo/si": _expect(
        "borrar_nav_wait_finish_text", "EstadoBorrarNavegacion", _DONE_OPTION
    ),
    "borrar_nav_explicar_motivo/no": _expect(
        "borrar_nav_how_to_text", "EstadoBorrarNavegacion"
    ),
    "borrar_nav_sabe_como/si": _expect(
        "borrar_nav_wait_finish_text", "EstadoBorrarNavegacion", _DONE_OPTION
    ),
    "borrar_nav_sabe_como/no": _expect(
        "borrar_nav_how_to_text", "EstadoBorrarNavegacion"
    ),
    "borrar_nav_esperando_confirmacion_directa/listo": _expect(
        "__registro_code__", "EstadoBorrarNavegacion", _RESULT_OPTIONS
    ),
    "borrar_nav_esperando_confirmacion_tras_explicar_como/listo": _expect(
        "borrar_nav_try_again_text", "EstadoBorrarNavegacion", _RESULT_OPTIONS
    ),
    **{
        f"{group}/funciono": _expect(
            "follow_up_help_text", "EstadoConsultaAdicional", _YES_NO_OPTIONS
        )
        for group in (
            "borrar_nav_finalizado_desde_registro",
            "borrar_nav_finalizado_desde_portal",
        )
    },
    **{
        f"{group}/no_funciono": _expect(
            "handoff_text", "EstadoSoporte", _BACK_OPTION, True
        )
        for group in (
            "borrar_nav_finalizado_desde_registro",
            "borrar_nav_finalizado_desde_portal",
        )
    },
}


def _response_options(result):
    if result.get("buttons"):
        return tuple(
            (button["reply"]["id"], button["reply"]["title"], None)
            for button in result["buttons"]
        )
    if result.get("list_config"):
        return tuple(
            (row["id"], row["title"], row.get("description"))
            for section in result["list_config"]["sections"]
            for row in section["rows"]
        )
    return ()


def _phase0_default(message_key):
    # Phase 0 ya contenía algunas claves repetidas; el fallback efectivo elegía la última.
    return next(
        message["default_content"]
        for message in reversed(message_store.DEFAULT_MESSAGES)
        if message["message_key"] == message_key
    )


@pytest.mark.parametrize("path", sorted(PHASE0_EXPECTED_PATHS))
def test_all_47_paths_keep_the_same_text_options_and_destination(path):
    assert len(PHASE0_EXPECTED_PATHS) == 47
    assert set(PHASE0_EXPECTED_PATHS) == {
        f"{row['option_group']}/{row['option_id']}"
        for row in message_store.DEFAULT_OPTION_BINDINGS
    }
    option_group, option_id = path.split("/", 1)
    context, state = _make_state(option_group)

    result = state.handle(context, f"button_{option_id}")
    reply_key, expected_state, expected_options, expected_handoff = PHASE0_EXPECTED_PATHS[path]
    if reply_key == "__registro_code__":
        expected_reply = (
            f"{_phase0_default('borrar_nav_registro_code_text')} MOTO123."
        )
    elif path == "formulario_pregunta_cargaste/no":
        expected_reply = (
            "Hay un formulario de registro que tenés que completar para avanzar. "
            "Una vez hecho eso, esperá 48 horas y volvé a intentar."
        )
    else:
        expected_reply = _phase0_default(reply_key)

    assert result["reply"] == expected_reply
    assert _response_options(result) == expected_options
    assert result["interactive_type"] == ("list" if expected_options == _MENU_OPTIONS else "button")
    assert context.get_state().state_name == expected_state
    assert result.get("handoff", False) is expected_handoff


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

    def test_preflujo_free_text_executes_its_only_option(self):
        context, state = _make_state("preflujo_desde_registro")

        state.handle(context, "quiero seguir")

        assert context.get_state().state_name == "EstadoPedirMail"
        assert context.get_var("flujo") == "registro"

    def test_preflujo_free_text_repeats_prompt_when_group_has_multiple_options(
        self, monkeypatch
    ):
        context, state = _make_state("preflujo_desde_registro")
        monkeypatch.setattr(
            message_store,
            "get_option_set",
            lambda group: [
                {
                    "option_id": "continuar",
                    "title": "CONTINUAR",
                    "button_title": "CONTINUAR",
                    "description": None,
                },
                {
                    "option_id": "otra",
                    "title": "OTRA OPCIÓN",
                    "button_title": "OTRA OPCIÓN",
                    "description": None,
                },
            ],
        )

        result = state.handle(context, "texto libre")

        assert context.get_state().state_name == "EstadoPreFlujo"
        assert result["reply"] == _phase0_default("pre_flujo_message")
        assert _response_options(result) == (
            ("continuar", "CONTINUAR", None),
            ("otra", "OTRA OPCIÓN", None),
        )

    def test_borrar_nav_listo_keeps_code_interpolation(self):
        context, state = _make_state("borrar_nav_esperando_confirmacion_directa")

        result = state.handle(context, "button_listo")

        assert context.get_var("clear_nav_step") == "finalizado"
        assert "MOTO123" in result["reply"]

    @pytest.mark.parametrize(
        "source_group, expected_intro, result_group",
        [
            (
                "descuentos_pregunta_cuando",
                "descuentos_login_steps_text",
                "pasos_resultado_desde_descuentos",
            ),
            (
                "formulario_pregunta_cuando",
                "formulario_login_steps_text",
                "pasos_resultado_desde_formulario",
            ),
        ],
    )
    def test_staged_login_sequence_keeps_four_messages_before_options(
        self, source_group, expected_intro, result_group
    ):
        context, source_state = _make_state(source_group)

        intro = source_state.handle(context, "button_mas_48")
        steps_state = context.get_state()
        step1 = steps_state.handle(context, "seguir")
        step2 = steps_state.handle(context, "seguir")
        step3 = steps_state.handle(context, "seguir")

        assert [intro["reply"], step1["reply"], step2["reply"], step3["reply"]] == [
            _phase0_default(expected_intro),
            _phase0_default("pasos_step1_text"),
            _phase0_default("pasos_step2_text"),
            _phase0_default("pasos_step3_text"),
        ]
        assert intro["buttons"] is None
        assert step1["buttons"] is None
        assert step2["buttons"] is None
        assert _response_options(step3) == _RESULT_OPTIONS
        assert steps_state.get_option_group(context) == result_group

    def test_borrar_nav_after_how_sequence_waits_before_showing_listo(self):
        context, state = _make_state("borrar_nav_sabe_como")

        instructions = state.handle(context, "button_no")
        confirmation = state.handle(context, "seguir")

        assert instructions["reply"] == _phase0_default("borrar_nav_how_to_text")
        assert instructions["buttons"] is None
        assert confirmation["reply"] == _phase0_default("borrar_nav_ask_finished_text")
        assert _response_options(confirmation) == _DONE_OPTION
        assert state.get_option_group(context) == (
            "borrar_nav_esperando_confirmacion_tras_explicar_como"
        )

    def test_private_step3_prompt_edit_does_not_leak_between_origins(self, monkeypatch):
        original_get_message = message_store.get_message
        monkeypatch.setattr(
            message_store,
            "get_message",
            lambda key, default="": (
                "Paso 3 editado solo en descuentos"
                if key == "pasos_resultado_desde_descuentos_prompt_text"
                else original_get_message(key, default=default)
            ),
        )
        discount_context, discount_state = _make_state("pasos_resultado_desde_descuentos")
        form_context, form_state = _make_state("pasos_resultado_desde_formulario")
        discount_context.set_var("login_step", 2)
        form_context.set_var("login_step", 2)

        discount_result = discount_state.handle(discount_context, "seguir")
        form_result = form_state.handle(form_context, "seguir")

        assert discount_result["reply"] == "Paso 3 editado solo en descuentos"
        assert form_result["reply"] == _phase0_default("pasos_step3_text")

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
