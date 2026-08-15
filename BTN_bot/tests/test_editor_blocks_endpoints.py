"""Contract and regression tests for the composed editor API."""

import copy

import pytest
from fastapi.testclient import TestClient

import bot
import editor_blocks
import message_store


@pytest.fixture
def client():
    return TestClient(bot.app)


def _blocks_by_id(payload):
    return {block["block_id"]: block for block in payload["blocks"]}


class TestEditorBlocksRead:
    def test_get_returns_complete_resolved_projection_in_bfs_order(self, client):
        response = client.get("/editor/blocks")

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["source"] == "default"
        assert payload["count"] == 33
        assert payload["storage"] == {
            "source": "default",
            "writable": False,
            "message": message_store.EDITOR_STORAGE_UNAVAILABLE_MESSAGE,
        }
        assert [block["orden_recorrido"] for block in payload["blocks"]] == list(
            range(1, 34)
        )
        assert payload["blocks"][0]["block_id"] == "menu_principal"
        assert [block["block_id"] for block in payload["blocks"][1:5]] == [
            "preflujo_desde_registro",
            "preflujo_desde_precios",
            "preflujo_desde_descuentos",
            "preflujo_desde_info_pedido",
        ]
        assert all(
            block["grupo_recorrido"] == "recorrido_principal"
            for block in payload["blocks"]
        )

    def test_real_list_buttons_previous_messages_and_text_blocks(self, client):
        blocks = _blocks_by_id(client.get("/editor/blocks").json())

        menu = blocks["menu_principal"]
        assert menu["formato"] == "lista"
        assert menu["formato_motivo"] == "4 opciones"
        assert menu["acepta_nuevas_opciones"] is True
        assert menu["limites_alta"] == {
            "titulo_botones": 20,
            "titulo_lista": 24,
            "descripcion_lista": 72,
            "respuesta": 2000,
        }
        assert menu["presentacion"]["texto_boton_limite_caracteres"] == 20
        assert menu["presentacion"]["titulo_seccion_limite_caracteres"] == 24

        info = blocks["info_pedido_opciones"]
        assert info["formato"] == "botones"
        assert info["formato_motivo"] == "2 opciones"

        steps = blocks["pasos_resultado_desde_descuentos"]
        assert [message["message_key"] for message in steps["mensajes_previos"]] == [
            "descuentos_login_steps_text",
            "pasos_step1_text",
            "pasos_step2_text",
        ]
        for message in steps["mensajes_previos"][1:]:
            assert message["compartido"] is True
            assert message["compartido_con"] == [{
                "block_id": "pasos_resultado_desde_formulario",
                "titulo": "Resultado de los pasos para formulario",
            }]

        email = blocks["pedido_email_registro"]
        assert email["prompt"]["message_key"] == "registro_intro_text"
        assert email["tiene_opciones"] is False
        assert email["acepta_nuevas_opciones"] is False
        assert email["formato"] == "texto_libre"
        assert email["opciones"] == []
        assert "ask_email_text" not in {
            block["prompt"]["message_key"] for block in blocks.values()
        }

    def test_effective_limit_is_minimum_when_title_key_has_two_roles(self, client):
        menu = _blocks_by_id(client.get("/editor/blocks").json())["menu_principal"]
        prices = next(
            option for option in menu["opciones"] if option["option_id"] == "no_veo_precios"
        )
        registration = next(
            option for option in menu["opciones"] if option["option_id"] == "registro"
        )

        assert prices["titulo_key"] == prices["titulo_boton_key"]
        assert prices["titulo_limite_caracteres"] == 20
        assert prices["titulo_boton_limite_caracteres"] == 20
        assert registration["titulo_key"] != registration["titulo_boton_key"]
        assert registration["titulo_limite_caracteres"] == 24
        assert registration["titulo_boton_limite_caracteres"] == 20

    def test_options_do_not_expose_required_response_flags(self, client):
        blocks = _blocks_by_id(client.get("/editor/blocks").json())

        for block in blocks.values():
            for option in block["opciones"]:
                assert "respuesta_obligatoria" not in option
                assert "respuesta_obligatoria_motivo" not in option

    def test_preflow_groups_are_editable_and_all_destinations_are_resolved(self, client):
        blocks = _blocks_by_id(client.get("/editor/blocks").json())

        expected = {
            "preflujo_desde_registro": (
                "pedido_email_registro", "Pedido de email para registro"
            ),
            "preflujo_desde_precios": (
                "pedido_email_precios", "Pedido de email para consultar precios"
            ),
            "preflujo_desde_descuentos": (
                "descuentos_pregunta_cargaste", "Formulario de descuentos"
            ),
            "preflujo_desde_info_pedido": (
                "info_pedido_opciones", "Información del pedido"
            ),
        }
        for block_id, (destination_id, destination_label) in expected.items():
            block = blocks[block_id]
            assert block["formato"] == "botones"
            assert block["acepta_nuevas_opciones"] is True
            assert block["prompt"]["message_key"] == f"{block_id}_prompt_text"
            assert len(block["opciones"]) == 1
            option = block["opciones"][0]
            assert option["editable"] is True
            assert option["sintetica"] is False
            assert option["lleva_a"]["block_id"] == destination_id
            assert option["lleva_a"]["label"] == destination_label

        menu_destinations = {
            option["option_id"]: option["lleva_a"]["block_id"]
            for option in blocks["menu_principal"]["opciones"]
        }
        assert menu_destinations == message_store.PREFLOW_ENTRY_GROUP_BY_MENU_OPTION

        delete_wait = blocks["borrar_nav_esperando_confirmacion_directa"]
        destination = delete_wait["opciones"][0]["lleva_a"]
        assert destination["es_condicional"] is True
        assert {item["block_id"] for item in destination["destinos_posibles"]} == {
            "borrar_nav_finalizado_desde_registro",
            "borrar_nav_finalizado_desde_portal",
        }

        for block in blocks.values():
            for option in block["opciones"]:
                destination = option.get("lleva_a") or {}
                if destination.get("es_condicional"):
                    assert all(
                        item["block_id"] in blocks
                        for item in destination["destinos_posibles"]
                    )
                else:
                    assert destination["block_id"] in blocks
                    assert destination["label"] == blocks[destination["block_id"]]["titulo"]

    def test_ambiguous_post_destinations_resolve_to_group_or_email_context(self):
        steps = editor_blocks._resolve_post_destination({
            "state_id": "EstadoPasosInicioSesion",
            "block_id": "pasos_resultado_desde_formulario",
        })
        email = editor_blocks._resolve_post_destination({
            "state_id": "EstadoPedirMail",
            "block_id": "pedido_email_registro",
        })
        form_substep = editor_blocks._resolve_post_destination({
            "state_id": "EstadoFormulario",
            "block_id": "formulario_pregunta_cuando",
        })
        terminal = editor_blocks._resolve_post_destination({
            "state_id": "EstadoFormulario",
            "block_id": "formulario_sin_cargar",
        })
        preflow = editor_blocks._resolve_post_destination({
            "state_id": "EstadoPreFlujo",
            "block_id": "preflujo_desde_registro",
        })

        assert steps["target_option_group"] == "pasos_resultado_desde_formulario"
        assert steps["target_vars"] is None
        assert email["target_option_group"] is None
        assert email["target_vars"] == {"flujo": "registro"}
        assert form_substep["target_option_group"] == "formulario_pregunta_cuando"
        assert form_substep["target_vars"] == {"form_substep": "pregunta_cuando"}
        assert terminal["target_option_group"] is None
        assert terminal["target_vars"] == {
            "form_substep": "fin",
            "editor_target_block": "formulario_sin_cargar",
        }
        assert preflow["target_option_group"] == "preflujo_desde_registro"
        assert preflow["target_vars"] is None

        with pytest.raises(editor_blocks.EditorBlockError, match="más de un bloque"):
            editor_blocks._resolve_post_destination("EstadoPasosInicioSesion")
        with pytest.raises(editor_blocks.EditorBlockError, match="más de un bloque"):
            editor_blocks._resolve_post_destination("EstadoPreFlujo")

    def test_bfs_shortest_path_cycle_cut_and_unreachable_grouping(self):
        def block(block_id, destinations=()):
            return {
                "block_id": block_id,
                "titulo": block_id,
                "prompt": {"message_key": "welcome_message"},
                "opciones": [
                    {
                        "option_id": str(index),
                        "orden": index,
                        "lleva_a": {"block_id": destination},
                    }
                    for index, destination in enumerate(destinations, start=1)
                ],
            }

        blocks = {
            "menu_principal": block("menu_principal", ["a", "b"]),
            "a": block("a", ["menu_principal", "b"]),
            "b": block("b"),
            "z": block("z"),
        }

        ordered = editor_blocks._order_blocks(blocks)

        assert [item["block_id"] for item in ordered] == ["menu_principal", "a", "b", "z"]
        assert ordered[2]["orden_recorrido"] == 3
        assert ordered[3]["grupo_recorrido"] == "otros_momentos"


@pytest.fixture
def composed_store(monkeypatch):
    messages = {
        "demo_prompt": {
            "message_key": "demo_prompt",
            "message_type": "text",
            "label": "Pregunta demo",
            "content": "Elegí una opción",
            "default_content": "Elegí una opción",
            "orden": 10,
        },
        "demo_button_text": {
            "message_key": "demo_button_text",
            "message_type": "button_text",
            "label": "Abrir demo",
            "content": "Ver opciones",
            "default_content": "Ver opciones",
            "orden": 20,
        },
        "demo_section_title": {
            "message_key": "demo_section_title",
            "message_type": "list_section_title",
            "label": "Sección demo",
            "content": "Opciones",
            "default_content": "Opciones",
            "orden": 30,
        },
        "existing_reply": {
            "message_key": "existing_reply",
            "message_type": "text",
            "label": "Respuesta existente",
            "content": "Respuesta ya creada",
            "default_content": "Respuesta ya creada",
            "orden": 40,
        },
    }
    options = []
    for index in range(1, 4):
        key = f"demo_{index}_title"
        messages[key] = {
            "message_key": key,
            "message_type": "button",
            "label": f"Opción {index}",
            "content": f"Opción {index}",
            "default_content": f"Opción {index}",
            "orden": index * 10,
        }
        options.append({
            "option_group": "demo",
            "option_id": str(index),
            "orden": index * 10,
            "title_key": key,
            "button_title_key": key,
            "description_key": None,
            "target_state": "EstadoSoporte",
            "target_vars": None,
            "target_option_group": None,
            "stay_in_state": False,
            "target_substep_key": None,
            "target_substep_value": None,
            "reply_key": None,
            "extra_flags": None,
        })

    def get_groups():
        return [{
            "option_group": "demo",
            "option_count": len(options),
            "interactive_type": "list" if len(options) >= 4 else "button",
        }]

    def get_config(group):
        assert group == "demo"
        return {
            "option_group": "demo",
            "button_text_key": "demo_button_text",
            "section_title_key": "demo_section_title",
            "prompt_key": "demo_prompt",
            "prev_message_keys": [],
            "shared_prev_keys": [],
            "button_text": messages["demo_button_text"]["content"],
            "section_title": messages["demo_section_title"]["content"],
        }

    def get_options(group):
        if group != "demo":
            return []
        resolved = []
        for raw in sorted(options, key=lambda item: (item["orden"], item["option_id"])):
            item = copy.deepcopy(raw)
            item["title"] = messages[item["title_key"]]["content"]
            button_key = item.get("button_title_key") or item["title_key"]
            item["button_title"] = messages[button_key]["content"]
            description_key = item.get("description_key")
            item["description"] = messages[description_key]["content"] if description_key else None
            resolved.append(item)
        return resolved

    def create_message(
        message_key, message_type, state_name, label, content,
        default_content=None, updated_by="", flujo_identificacion_mensaje=None, orden=None,
    ):
        if message_key in messages:
            raise ValueError(f"message_key '{message_key}' ya existe")
        messages[message_key] = {
            "message_key": message_key,
            "message_type": message_type,
            "state_name": state_name,
            "label": label,
            "content": content,
            "default_content": default_content or content,
            "orden": orden or 100,
            "updated_by": updated_by,
        }

    def create_binding(group, option_id, orden, title_key, **kwargs):
        options.append({
            "option_group": group,
            "option_id": option_id,
            "orden": orden,
            "title_key": title_key,
            "button_title_key": kwargs.get("button_title_key"),
            "description_key": kwargs.get("description_key"),
            "target_state": kwargs.get("target_state"),
            "target_vars": kwargs.get("target_vars"),
            "target_option_group": kwargs.get("target_option_group"),
            "stay_in_state": False,
            "target_substep_key": None,
            "target_substep_value": None,
            "reply_key": kwargs.get("reply_key"),
            "extra_flags": None,
        })

    def update_binding(group, option_id, updated_by="", **changes):
        row = next(item for item in options if item["option_id"] == option_id)
        row.update(changes)

    def delete_message(message_key, updated_by=""):
        if message_key not in messages:
            raise KeyError(message_key)
        del messages[message_key]

    monkeypatch.setattr(editor_blocks.message_store, "get_option_groups", get_groups)
    monkeypatch.setattr(editor_blocks.message_store, "get_option_group_config", get_config)
    monkeypatch.setattr(editor_blocks.message_store, "get_option_set", get_options)
    monkeypatch.setattr(
        editor_blocks.message_store,
        "get_message_metadata",
        lambda key: copy.deepcopy(messages.get(key)),
    )
    monkeypatch.setattr(
        editor_blocks.message_store,
        "get_message",
        lambda key, default="": messages.get(key, {}).get("content", default),
    )
    monkeypatch.setattr(editor_blocks.message_store, "create_message", create_message)
    monkeypatch.setattr(editor_blocks.message_store, "create_option_binding", create_binding)
    monkeypatch.setattr(editor_blocks.message_store, "update_option_binding", update_binding)
    monkeypatch.setattr(editor_blocks.message_store, "delete_message", delete_message)
    monkeypatch.setattr(editor_blocks.message_store, "invalidate_cache", lambda: None)
    return TestClient(bot.app), messages, options


class TestEditorComposedPost:
    def test_validation_error_identifies_the_field(self, composed_store):
        client, _, _ = composed_store

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "x" * 25,
            "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
            "lleva_a": "EstadoSoporte",
            "posicion": 4,
            "permitir_cambio_a_lista": True,
        })

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "message": "titulo excede 24 caracteres",
            "field": "titulo",
            "code": "validation_error",
        }

    def test_creates_messages_and_option_and_fourth_item_becomes_list(self, composed_store):
        client, messages, options = composed_store

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "Garantías y reparaciones",
            "titulo_boton": "Garantías",
            "descripcion": "Cobertura y plazos",
            "respuesta": {"modo": "nuevo", "contenido": "Las garantías cubren fallas."},
            "lleva_a": "EstadoSoporte",
            "posicion": 4,
            "permitir_cambio_a_lista": True,
            "updated_by": "qa",
        })

        assert response.status_code == 201
        block = response.json()["block"]
        assert block["formato"] == "lista"
        assert block["formato_motivo"] == "4 opciones"
        created = next(
            option
            for option in options
            if option["option_id"] == "garantias_y_reparaciones"
        )
        assert created["title_key"] == "demo_garantias_y_reparaciones_title"
        assert created["button_title_key"] == (
            "demo_garantias_y_reparaciones_button_title"
        )
        assert created["description_key"] == (
            "demo_garantias_y_reparaciones_description"
        )
        assert created["reply_key"] == "demo_garantias_y_reparaciones_reply_text"
        assert created["target_state"] == "EstadoSoporte"
        assert messages[created["title_key"]]["message_type"] == "list_row_title"
        assert messages[created["title_key"]]["content"] == "Garantías y reparaciones"
        assert messages[created["button_title_key"]]["message_type"] == "button"
        assert messages[created["button_title_key"]]["content"] == "Garantías"
        assert "…" not in messages[created["button_title_key"]]["content"]
        assert messages[created["reply_key"]]["content"] == "Las garantías cubren fallas."
        for index in range(1, 4):
            transitioned = next(option for option in options if option["option_id"] == str(index))
            assert transitioned["title_key"] == f"demo_{index}_list_title"
            assert transitioned["button_title_key"] == f"demo_{index}_title"
            assert messages[transitioned["title_key"]]["content"] == f"Opción {index}"
        returned = next(
            option
            for option in block["opciones"]
            if option["option_id"] == "garantias_y_reparaciones"
        )
        assert returned["titulo_limite_caracteres"] == 24
        assert returned["titulo_boton_limite_caracteres"] == 20
        assert returned["respuesta"]["content"] == "Las garantías cubren fallas."
        assert returned["lleva_a"]["block_id"] == "derivacion_soporte"

    def test_existing_reply_mode_is_rejected_to_preserve_private_ownership(self, composed_store):
        client, messages, options = composed_store
        before_messages = copy.deepcopy(messages)
        before_options = copy.deepcopy(options)

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "Ayuda",
            "titulo_boton": "Ayuda",
            "descripcion": "Opciones de ayuda",
            "respuesta": {"modo": "existente", "message_key": "existing_reply"},
            "lleva_a": {"state_id": "EstadoSoporte", "block_id": "derivacion_soporte"},
            "posicion": 4,
            "permitir_cambio_a_lista": True,
        })

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "message": "respuesta.modo debe ser 'nuevo' o 'sin_respuesta'",
            "field": "respuesta.modo",
            "code": "validation_error",
        }
        assert messages == before_messages
        assert options == before_options

    def test_option_can_be_created_without_an_own_reply(self, composed_store):
        client, messages, options = composed_store

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "Continuar con soporte",
            "titulo_boton": "Continuar",
            "descripcion": "Ir al próximo momento",
            "respuesta": {"modo": "sin_respuesta"},
            "lleva_a": "EstadoSoporte",
            "posicion": 4,
            "permitir_cambio_a_lista": True,
        })

        assert response.status_code == 201
        created = next(option for option in options if option["option_id"] == "continuar_con_soporte")
        assert created["reply_key"] is None
        assert not any(key.startswith("demo_continuar_con_soporte_reply") for key in messages)
        returned = next(
            option
            for option in response.json()["block"]["opciones"]
            if option["option_id"] == "continuar_con_soporte"
        )
        assert returned["respuesta"] is None

    def test_response_can_be_added_and_then_logically_discarded(self, composed_store):
        client, messages, options = composed_store

        added = client.put("/editor/blocks/demo/opciones/1/respuesta", json={
            "respuesta": {"modo": "nuevo", "contenido": "Mensaje agregado después."},
            "updated_by": "qa",
        })

        assert added.status_code == 200
        option = next(item for item in options if item["option_id"] == "1")
        reply_key = option["reply_key"]
        assert reply_key.startswith("demo_1_reply_")
        assert messages[reply_key]["content"] == "Mensaje agregado después."

        removed = client.put("/editor/blocks/demo/opciones/1/respuesta", json={
            "respuesta": {"modo": "sin_respuesta"},
            "updated_by": "qa",
        })

        assert removed.status_code == 200
        assert option["reply_key"] is None
        assert reply_key not in messages
        returned = next(
            item for item in removed.json()["block"]["opciones"] if item["option_id"] == "1"
        )
        assert returned["respuesta"] is None

    def test_shared_editor_reply_is_kept_until_its_last_reference_is_removed(self, composed_store):
        client, messages, options = composed_store
        messages["shared_editor_reply"] = {
            "message_key": "shared_editor_reply",
            "message_type": "text",
            "label": "Respuesta al elegir: compartida",
            "content": "Respuesta compartida",
            "default_content": "Respuesta compartida",
            "orden": 100,
        }
        options[0]["reply_key"] = "shared_editor_reply"
        options[1]["reply_key"] = "shared_editor_reply"

        first = client.put("/editor/blocks/demo/opciones/1/respuesta", json={
            "respuesta": {"modo": "sin_respuesta"},
        })
        assert first.status_code == 200
        assert "shared_editor_reply" in messages

        second = client.put("/editor/blocks/demo/opciones/2/respuesta", json={
            "respuesta": {"modo": "sin_respuesta"},
        })
        assert second.status_code == 200
        assert "shared_editor_reply" not in messages

    def test_target_option_group_does_not_block_removing_a_normal_reply(self, composed_store):
        client, messages, options = composed_store
        options[0]["reply_key"] = "existing_reply"
        options[0]["target_option_group"] = "preflujo_desde_registro"

        response = client.put("/editor/blocks/demo/opciones/1/respuesta", json={
            "respuesta": {"modo": "sin_respuesta"},
            "updated_by": "qa",
        })

        assert response.status_code == 200
        assert options[0]["reply_key"] is None
        assert "existing_reply" in messages

    def test_explicit_terminal_block_persists_substep_and_keeps_exact_link(self, composed_store):
        client, _, options = composed_store

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "Terminar",
            "titulo_boton": "Terminar",
            "descripcion": "Cerrar este recorrido",
            "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
            "lleva_a": {
                "state_id": "EstadoFormulario",
                "block_id": "formulario_sin_cargar",
            },
            "posicion": 4,
            "permitir_cambio_a_lista": True,
        })

        assert response.status_code == 201
        created = next(option for option in options if option["option_id"] == "terminar")
        assert created["target_vars"] == {
            "form_substep": "fin",
            "editor_target_block": "formulario_sin_cargar",
        }
        returned = next(
            option
            for option in response.json()["block"]["opciones"]
            if option["option_id"] == "terminar"
        )
        assert returned["lleva_a"]["block_id"] == "formulario_sin_cargar"

    @pytest.mark.parametrize(
        ("path", "body", "expected_status"),
        [
            (
                "/editor/blocks/demo/opciones",
                {
                    "titulo": "Cuarta",
                    "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
                    "lleva_a": "EstadoSoporte",
                    "posicion": 4,
                },
                409,
            ),
            (
                "/editor/blocks/pedido_email_registro/opciones",
                {
                    "titulo": "Nueva",
                    "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
                    "lleva_a": "EstadoSoporte",
                    "posicion": 1,
                },
                409,
            ),
            (
                "/editor/blocks/demo/opciones",
                {
                    "titulo": "Nueva",
                    "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
                    "lleva_a": "EstadoPedirMail",
                    "posicion": 4,
                    "permitir_cambio_a_lista": True,
                },
                422,
            ),
            (
                "/editor/blocks/demo/opciones",
                {
                    "titulo": "x" * 25,
                    "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
                    "lleva_a": "EstadoSoporte",
                    "posicion": 4,
                    "permitir_cambio_a_lista": True,
                },
                422,
            ),
            (
                "/editor/blocks/demo/opciones",
                {
                    "titulo": "Nueva",
                    "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
                    "lleva_a": "EstadoSoporte",
                    "posicion": 5,
                    "permitir_cambio_a_lista": True,
                },
                422,
            ),
        ],
    )
    def test_contract_validations(self, composed_store, path, body, expected_status):
        client, _, _ = composed_store
        response = client.post(path, json=body)
        assert response.status_code == expected_status

    def test_ten_option_cap_returns_409(self, composed_store):
        client, _, options = composed_store
        for index in range(4, 11):
            options.append({
                **copy.deepcopy(options[0]),
                "option_id": str(index),
                "orden": index * 10,
            })

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "Una más",
            "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
            "lleva_a": "EstadoSoporte",
            "posicion": 11,
        })

        assert response.status_code == 409

    def test_persistence_failure_maps_to_503(self, composed_store, monkeypatch):
        client, _, _ = composed_store
        monkeypatch.setattr(
            editor_blocks.message_store,
            "create_message",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                message_store.EditorStorageUnavailable()
            ),
        )

        response = client.post("/editor/blocks/demo/opciones", json={
            "titulo": "Nueva",
            "titulo_boton": "Nueva",
            "descripcion": "Descripción nueva",
            "respuesta": {"modo": "nuevo", "contenido": "Respuesta privada"},
            "lleva_a": "EstadoSoporte",
            "posicion": 4,
            "permitir_cambio_a_lista": True,
        })

        assert response.status_code == 503
        assert response.json()["detail"] == {
            "message": message_store.EDITOR_STORAGE_UNAVAILABLE_MESSAGE,
            "field": None,
            "code": "storage_unavailable",
        }
