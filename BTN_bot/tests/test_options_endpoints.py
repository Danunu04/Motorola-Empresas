"""Tests permanentes de los siete endpoints administrativos de /options."""

import copy

import pytest
from fastapi.testclient import TestClient

import bot


def _option(option_id, orden):
    return {
        "option_group": "demo",
        "option_id": option_id,
        "orden": orden,
        "title_key": "generic_yes_button",
        "button_title_key": None,
        "description_key": None,
        "target_state": "EstadoInicial",
        "target_vars": None,
        "target_option_group": None,
        "stay_in_state": False,
        "target_substep_key": None,
        "target_substep_value": None,
        "reply_key": None,
        "extra_flags": None,
        "title": f"Opción {option_id}",
        "button_title": f"Opción {option_id}",
        "description": None,
    }


@pytest.fixture
def option_api(monkeypatch):
    options = {"demo": [_option(str(index), index * 10) for index in range(1, 4)]}
    configs = {
        "demo": {
            "option_group": "demo",
            "button_text_key": "generic_options_button_text",
            "section_title_key": "generic_options_section_title",
            "prompt_key": "welcome_message",
            "prev_message_keys": [],
            "shared_prev_keys": [],
            "button_text": "Ver opciones",
            "section_title": "Opciones",
        }
    }

    def get_option_set(group):
        return copy.deepcopy(
            sorted(options.get(group, []), key=lambda row: (row["orden"], row["option_id"]))
        )

    def get_config(group):
        return copy.deepcopy(configs.get(group, {
            "option_group": group,
            "button_text_key": "generic_options_button_text",
            "section_title_key": "generic_options_section_title",
            "prompt_key": "welcome_message",
            "prev_message_keys": [],
            "shared_prev_keys": [],
            "button_text": "Ver opciones",
            "section_title": "Opciones",
            "source": "default-fallback",
        }))

    def get_groups():
        groups = []
        for group in sorted(set(options) | set(configs)):
            count = len(options.get(group, []))
            config = get_config(group)
            groups.append({
                "option_group": group,
                "option_count": count,
                "interactive_type": "list" if count > 3 else ("button" if count else None),
                "button_text_key": config["button_text_key"],
                "section_title_key": config["section_title_key"],
                "prompt_key": config["prompt_key"],
                "prev_message_keys": config["prev_message_keys"],
                "shared_prev_keys": config["shared_prev_keys"],
            })
        return groups

    def set_config(
        group, button_text_key, section_title_key, prompt_key,
        prev_message_keys=None, shared_prev_keys=None, updated_by="",
    ):
        configs[group] = {
            "option_group": group,
            "button_text_key": button_text_key,
            "section_title_key": section_title_key,
            "prompt_key": prompt_key,
            "prev_message_keys": prev_message_keys or [],
            "shared_prev_keys": shared_prev_keys or [],
            "button_text": "Abrir",
            "section_title": "Alternativas",
            "updated_by": updated_by,
        }

    def create(group, option_id, orden, title_key, **kwargs):
        rows = options.setdefault(group, [])
        if any(row["option_id"] == option_id for row in rows):
            raise ValueError(f"option_id '{option_id}' ya existe en '{group}'")
        if len(rows) >= 10:
            raise ValueError("un option_group no puede superar 10 opciones activas")
        row = _option(option_id, orden)
        row.update({
            "option_group": group,
            "title_key": title_key,
            "button_title_key": kwargs.get("button_title_key"),
            "description_key": kwargs.get("description_key"),
            "target_state": kwargs.get("target_state"),
            "target_vars": kwargs.get("target_vars"),
            "target_option_group": kwargs.get("target_option_group"),
            "stay_in_state": kwargs.get("stay_in_state", False),
            "target_substep_key": kwargs.get("target_substep_key"),
            "target_substep_value": kwargs.get("target_substep_value"),
            "reply_key": kwargs.get("reply_key"),
            "extra_flags": kwargs.get("extra_flags"),
        })
        rows.append(row)

    def update(group, option_id, updated_by="", **changes):
        row = next(
            (row for row in options.get(group, []) if row["option_id"] == option_id),
            None,
        )
        if row is None:
            raise KeyError((group, option_id))
        row.update(changes)

    def delete(group, option_id, updated_by=""):
        rows = options.get(group, [])
        row = next((row for row in rows if row["option_id"] == option_id), None)
        if row is None:
            raise KeyError((group, option_id))
        if len(rows) <= 1:
            raise ValueError("no se puede eliminar la ultima opcion activa de un grupo")
        rows.remove(row)

    monkeypatch.setattr(bot.message_store, "get_option_groups", get_groups)
    monkeypatch.setattr(bot.message_store, "get_option_set", get_option_set)
    monkeypatch.setattr(bot.message_store, "get_option_group_config", get_config)
    monkeypatch.setattr(bot.message_store, "set_option_group_config", set_config)
    monkeypatch.setattr(bot.message_store, "create_option_binding", create)
    monkeypatch.setattr(bot.message_store, "update_option_binding", update)
    monkeypatch.setattr(bot.message_store, "delete_option_binding", delete)

    return TestClient(bot.app), options, configs


class TestOptionRouteRegistration:
    def test_exact_seven_routes_and_static_routes_come_first(self):
        routes = [
            (route.path, frozenset(route.methods))
            for route in bot.app.routes
            if route.path.startswith("/options")
        ]
        assert routes == [
            ("/options/groups", frozenset({"GET"})),
            ("/options/groups/{option_group}/config", frozenset({"GET"})),
            ("/options/groups/{option_group}/config", frozenset({"PUT"})),
            ("/options/{option_group}", frozenset({"GET"})),
            ("/options/{option_group}", frozenset({"POST"})),
            ("/options/{option_group}/{option_id}", frozenset({"PUT"})),
            ("/options/{option_group}/{option_id}", frozenset({"DELETE"})),
        ]

    def test_flow_states_exposes_all_14_business_labels(self):
        response = TestClient(bot.app).get("/flow/states")

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 14
        assert payload["states"] == [
            {"state_id": state_id, "label": bot.FLOW_STATE_LABELS[state_id]}
            for state_id in bot.StateFactory.get_registered_states()
        ]


class TestOptionReadsAndConfig:
    def test_groups_route_is_not_shadowed_by_dynamic_group_route(self, option_api):
        client, _, _ = option_api
        response = client.get("/options/groups")
        assert response.status_code == 200
        assert response.json()["source"] == "default"
        assert response.json()["groups"][0]["option_group"] == "demo"

    def test_group_options_include_derived_format(self, option_api):
        client, _, _ = option_api
        response = client.get("/options/demo")
        assert response.status_code == 200
        assert response.json()["source"] == "default"
        assert response.json()["option_count"] == 3
        assert response.json()["interactive_type"] == "button"

    def test_get_and_put_group_config(self, option_api):
        client, _, _ = option_api
        response = client.get("/options/groups/demo/config")
        assert response.status_code == 200
        assert response.json()["source"] == "default"
        assert response.json()["config"]["section_title"] == "Opciones"

        response = client.put("/options/groups/demo/config", json={
            "button_text_key": "custom_button",
            "section_title_key": "custom_section",
            "prompt_key": "welcome_message",
            "prev_message_keys": ["pasos_step1_text"],
            "shared_prev_keys": ["pasos_step1_text"],
            "updated_by": "qa",
        })
        assert response.status_code == 200
        assert response.json()["config"]["button_text"] == "Abrir"
        assert response.json()["config"]["prev_message_keys"] == ["pasos_step1_text"]

    def test_config_validation_and_persistence_errors_map_to_422_and_503(
        self, option_api, monkeypatch
    ):
        client, _, _ = option_api
        monkeypatch.setattr(
            bot.message_store,
            "set_option_group_config",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("key inexistente")),
        )
        assert client.put("/options/groups/demo/config", json={
            "button_text_key": "x", "section_title_key": "y", "prompt_key": "z"
        }).status_code == 422

        monkeypatch.setattr(
            bot.message_store,
            "set_option_group_config",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                bot.message_store.EditorStorageUnavailable()
            ),
        )
        unavailable = client.put("/options/groups/demo/config", json={
            "button_text_key": "x", "section_title_key": "y", "prompt_key": "z"
        })
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == {
            "message": bot.message_store.EDITOR_STORAGE_UNAVAILABLE_MESSAGE,
            "field": None,
            "code": "storage_unavailable",
        }


class TestOptionMutations:
    def test_post_fourth_option_changes_buttons_to_list(self, option_api):
        client, _, _ = option_api
        response = client.post("/options/demo", json={
            "option_id": "4",
            "orden": 40,
            "title_key": "generic_yes_button",
            "target_state": "EstadoInicial",
            "updated_by": "qa",
        })
        assert response.status_code == 201
        assert response.json()["option_count"] == 4
        assert response.json()["interactive_type"] == "list"

    def test_post_accepts_deferred_target_option_group(self, option_api):
        client, options, _ = option_api
        response = client.post("/options/demo", json={
            "option_id": "4",
            "orden": 40,
            "title_key": "generic_yes_button",
            "target_state": "EstadoPasosInicioSesion",
            "target_option_group": "pasos_resultado_desde_login",
        })

        assert response.status_code == 201
        assert options["demo"][-1]["target_option_group"] == "pasos_resultado_desde_login"

    def test_delete_fourth_option_changes_list_back_to_buttons(self, option_api):
        client, _, _ = option_api
        client.post("/options/demo", json={
            "option_id": "4", "orden": 40, "title_key": "generic_yes_button",
            "target_state": "EstadoInicial",
        })
        response = client.delete("/options/demo/4?updated_by=qa")
        assert response.status_code == 200
        assert response.json()["option_count"] == 3
        assert response.json()["interactive_type"] == "button"

    def test_put_merges_only_sent_fields_and_reorders(self, option_api):
        client, _, _ = option_api
        response = client.put("/options/demo/3", json={"orden": 5, "updated_by": "qa"})
        assert response.status_code == 200
        assert response.json()["options"][0]["option_id"] == "3"

    def test_update_missing_or_empty_returns_404_or_422(self, option_api):
        client, _, _ = option_api
        assert client.put("/options/demo/missing", json={"orden": 5}).status_code == 404
        assert client.put("/options/demo/1", json={"updated_by": "qa"}).status_code == 422

    def test_delete_missing_or_last_returns_404_or_409(self, option_api):
        client, options, _ = option_api
        assert client.delete("/options/demo/missing").status_code == 404
        options["single"] = [{**_option("only", 10), "option_group": "single"}]
        assert client.delete("/options/single/only").status_code == 409


class TestOptionValidationErrors:
    def test_order_is_required_and_interactive_type_is_forbidden(self, option_api):
        client, _, _ = option_api
        base = {
            "option_id": "4",
            "title_key": "generic_yes_button",
            "target_state": "EstadoInicial",
        }
        assert client.post("/options/demo", json=base).status_code == 422
        assert client.post("/options/demo", json={
            **base, "orden": 40, "interactive_type": "list"
        }).status_code == 422

    def test_invalid_target_state_returns_422(self, option_api):
        client, _, _ = option_api
        response = client.post("/options/demo", json={
            "option_id": "4", "orden": 40, "title_key": "generic_yes_button",
            "target_state": "EstadoInventado",
        })
        assert response.status_code == 422

    def test_duplicate_and_eleventh_option_return_409(self, option_api):
        client, options, _ = option_api
        duplicate = client.post("/options/demo", json={
            "option_id": "1", "orden": 50, "title_key": "generic_yes_button",
            "target_state": "EstadoInicial",
        })
        assert duplicate.status_code == 409

        options["full"] = [
            {**_option(str(index), index), "option_group": "full"}
            for index in range(10)
        ]
        eleventh = client.post("/options/full", json={
            "option_id": "11", "orden": 11, "title_key": "generic_yes_button",
            "target_state": "EstadoInicial",
        })
        assert eleventh.status_code == 409

    def test_runtime_persistence_error_returns_503(self, option_api, monkeypatch):
        client, _, _ = option_api
        monkeypatch.setattr(
            bot.message_store,
            "create_option_binding",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                bot.message_store.EditorStorageUnavailable()
            ),
        )
        response = client.post("/options/demo", json={
            "option_id": "4", "orden": 40, "title_key": "generic_yes_button",
            "target_state": "EstadoInicial",
        })
        assert response.status_code == 503
        assert response.json()["detail"]["message"] == (
            bot.message_store.EDITOR_STORAGE_UNAVAILABLE_MESSAGE
        )
