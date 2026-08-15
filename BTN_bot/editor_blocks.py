"""Read/write composition layer for the business-facing conversation editor.

This module deliberately does not participate in the bot runtime.  It projects the
message/option tables into editor blocks, calculates their traversal order and
coordinates the existing fine-grained stores for the composed option creation API.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

import message_store
from flow_states_btn import FLOW_STATE_LABELS


class EditorBlockError(ValueError):
    """Base class for contract validation failures."""

    default_code = "validation_error"

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.code = code or self.default_code


class EditorBlockNotFound(EditorBlockError):
    """Raised when a requested block does not exist."""

    default_code = "not_found"


class EditorBlockConflict(EditorBlockError):
    """Raised when a valid request conflicts with the current model state."""

    default_code = "conflict"


OPTION_GROUP_META: Dict[str, Tuple[str, str]] = {
    "menu_principal": ("Menú principal", "EstadoInicial"),
    "preflujo_desde_registro": (
        "Mensaje de espera para registro",
        "EstadoPreFlujo",
    ),
    "preflujo_desde_precios": (
        "Mensaje de espera para consultar precios",
        "EstadoPreFlujo",
    ),
    "preflujo_desde_descuentos": (
        "Mensaje de espera para descuentos",
        "EstadoPreFlujo",
    ),
    "preflujo_desde_info_pedido": (
        "Mensaje de espera para información del pedido",
        "EstadoPreFlujo",
    ),
    "info_pedido_opciones": ("Información del pedido", "EstadoInfoPedido"),
    "registro_mail_empresa_opciones": (
        "Validación del email empresarial",
        "EstadoRegistroMailEmpresa",
    ),
    "login_opciones": ("Consulta sobre ingreso anterior", "EstadoLogin"),
    "pasos_resultado_desde_descuentos": (
        "Resultado de los pasos para descuentos",
        "EstadoPasosInicioSesion",
    ),
    "pasos_resultado_desde_login": (
        "Resultado de los pasos desde ingreso anterior",
        "EstadoPasosInicioSesion",
    ),
    "pasos_resultado_desde_formulario": (
        "Resultado de los pasos para formulario",
        "EstadoPasosInicioSesion",
    ),
    "consulta_adicional_opciones": ("Consulta adicional", "EstadoConsultaAdicional"),
    "finalizado_opciones": ("Fin de la conversación", "EstadoFinalizado"),
    "portal_beneficios_opciones": (
        "Consulta sobre el portal de beneficios",
        "EstadoPortalBeneficios",
    ),
    "descuentos_pregunta_cargaste": (
        "Formulario de descuentos",
        "EstadoNoVeoDescuentos",
    ),
    "descuentos_pregunta_cuando": (
        "Antigüedad del formulario de descuentos",
        "EstadoNoVeoDescuentos",
    ),
    "formulario_pregunta_cargaste": ("Formulario de registro", "EstadoFormulario"),
    "formulario_pregunta_cuando": (
        "Antigüedad del formulario de registro",
        "EstadoFormulario",
    ),
    "borrar_nav_confirmar_desde_registro": (
        "Confirmar borrado desde registro",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_confirmar_desde_portal": (
        "Confirmar borrado desde el portal",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_explicar_motivo": (
        "Explicación del borrado de navegación",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_sabe_como": (
        "Consulta sobre cómo borrar la navegación",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_esperando_confirmacion_directa": (
        "Confirmación directa del borrado",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_esperando_confirmacion_tras_explicar_como": (
        "Confirmación después de explicar el borrado",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_finalizado_desde_registro": (
        "Resultado del borrado desde registro",
        "EstadoBorrarNavegacion",
    ),
    "borrar_nav_finalizado_desde_portal": (
        "Resultado del borrado desde el portal",
        "EstadoBorrarNavegacion",
    ),
}


FIXED_BLOCK_SPECS: Dict[str, Dict[str, Any]] = {
    "pedido_email_registro": {
        "titulo": "Pedido de email para registro",
        "state_id": "EstadoPedirMail",
        "prompt_key": "registro_intro_text",
        "formato": "texto_libre",
        "formato_motivo": "El usuario responde escribiendo",
    },
    "pedido_email_precios": {
        "titulo": "Pedido de email para consultar precios",
        "state_id": "EstadoPedirMail",
        "prompt_key": "precios_intro_text",
        "formato": "texto_libre",
        "formato_motivo": "El usuario responde escribiendo",
    },
    "descuentos_sin_formulario": {
        "titulo": "Indicación para completar el formulario de descuentos",
        "state_id": "EstadoNoVeoDescuentos",
        "prompt_key": "descuentos_not_loaded_text",
        "formato": "sin_opciones",
        "formato_motivo": "Fin de este recorrido",
    },
    "descuentos_esperar_48": {
        "titulo": "Espera del formulario de descuentos",
        "state_id": "EstadoNoVeoDescuentos",
        "prompt_key": "descuentos_wait_48_text",
        "formato": "sin_opciones",
        "formato_motivo": "Fin de este recorrido",
    },
    "formulario_sin_cargar": {
        "titulo": "Indicación para completar el formulario de registro",
        "state_id": "EstadoFormulario",
        "prompt_key": "not_loaded_form_text",
        "formato": "sin_opciones",
        "formato_motivo": "Fin de este recorrido",
    },
    "formulario_esperar_48": {
        "titulo": "Espera del formulario de registro",
        "state_id": "EstadoFormulario",
        "prompt_key": "wait_48_form_text",
        "formato": "sin_opciones",
        "formato_motivo": "Fin de este recorrido",
    },
    "derivacion_soporte": {
        "titulo": "Derivación a una persona",
        "state_id": "EstadoSoporte",
        "prompt_key": "handoff_text",
        "formato": "sin_opciones",
        "formato_motivo": "Derivación a atención humana",
    },
}


REPLY_KEY_BLOCKS = {
    spec["prompt_key"]: block_id
    for block_id, spec in FIXED_BLOCK_SPECS.items()
    if block_id not in {"pedido_email_registro", "pedido_email_precios"}
}


STATE_CANONICAL_BLOCK: Dict[str, Optional[str]] = {
    "EstadoInicial": "menu_principal",
    # The menu origin selects one of four independent waiting blocks.
    "EstadoPreFlujo": None,
    "EstadoNoVeoDescuentos": "descuentos_pregunta_cargaste",
    "EstadoInfoPedido": "info_pedido_opciones",
    # A single state receives two different entry messages.  The caller must choose one.
    "EstadoPedirMail": None,
    "EstadoRegistroMailEmpresa": "registro_mail_empresa_opciones",
    "EstadoLogin": "login_opciones",
    # The origin determines which private result group is shown.
    "EstadoPasosInicioSesion": None,
    "EstadoConsultaAdicional": "consulta_adicional_opciones",
    "EstadoFinalizado": "finalizado_opciones",
    "EstadoFormulario": "formulario_pregunta_cargaste",
    "EstadoPortalBeneficios": "portal_beneficios_opciones",
    # Registration and portal have distinct entry blocks.
    "EstadoBorrarNavegacion": None,
    "EstadoSoporte": "derivacion_soporte",
}


EDITOR_CREATE_LIMITS = {
    "titulo_botones": message_store.MESSAGE_LIMITS["button"],
    "titulo_lista": message_store.MESSAGE_LIMITS["list_row_title"],
    "descripcion_lista": message_store.MESSAGE_LIMITS["list_row_description"],
    "respuesta": message_store.MESSAGE_LIMITS["text"],
}


BLOCK_TARGET_VARS: Dict[str, Dict[str, Any]] = {
    "pedido_email_registro": {"flujo": "registro"},
    "pedido_email_precios": {"flujo": "precios"},
    "descuentos_pregunta_cargaste": {"descuentos_substep": "pregunta_cargaste"},
    "descuentos_pregunta_cuando": {"descuentos_substep": "pregunta_cuando"},
    "formulario_pregunta_cargaste": {"form_substep": "pregunta_cargaste"},
    "formulario_pregunta_cuando": {"form_substep": "pregunta_cuando"},
    "borrar_nav_confirmar_desde_registro": {
        "clear_nav_step": "confirmar",
        "flujo": "registro",
    },
    "borrar_nav_confirmar_desde_portal": {
        "clear_nav_step": "confirmar",
        "flujo": "precios",
    },
    "borrar_nav_explicar_motivo": {"clear_nav_step": "explicar_motivo"},
    "borrar_nav_sabe_como": {"clear_nav_step": "sabe_como"},
    "borrar_nav_esperando_confirmacion_directa": {
        "clear_nav_step": "esperando_confirmacion",
    },
    "borrar_nav_esperando_confirmacion_tras_explicar_como": {
        "clear_nav_step": "esperando_confirmacion",
    },
    "borrar_nav_finalizado_desde_registro": {
        "clear_nav_step": "finalizado",
        "flujo": "registro",
    },
    "borrar_nav_finalizado_desde_portal": {
        "clear_nav_step": "finalizado",
        "flujo": "precios",
    },
    # These terminal read blocks share a runtime substep.  The inert editor_target_block
    # marker keeps the read-model link exact when the composed POST creates a private reply.
    "descuentos_sin_formulario": {
        "descuentos_substep": "fin",
        "editor_target_block": "descuentos_sin_formulario",
    },
    "descuentos_esperar_48": {
        "descuentos_substep": "fin",
        "editor_target_block": "descuentos_esperar_48",
    },
    "formulario_sin_cargar": {
        "form_substep": "fin",
        "editor_target_block": "formulario_sin_cargar",
    },
    "formulario_esperar_48": {
        "form_substep": "fin",
        "editor_target_block": "formulario_esperar_48",
    },
}


EMAIL_GRAPH_NEIGHBORS = {
    "pedido_email_registro": [
        "registro_mail_empresa_opciones",
        "formulario_pregunta_cargaste",
    ],
    "pedido_email_precios": ["portal_beneficios_opciones"],
}


DELETE_RESULT_DESTINATIONS = [
    {
        "condicion": "El recorrido comenzó en registro",
        "state_id": "EstadoBorrarNavegacion",
        "block_id": "borrar_nav_finalizado_desde_registro",
    },
    {
        "condicion": "El recorrido comenzó en el portal de beneficios",
        "state_id": "EstadoBorrarNavegacion",
        "block_id": "borrar_nav_finalizado_desde_portal",
    },
]


_DEFAULT_MESSAGE_BY_KEY = {
    item["message_key"]: item for item in message_store.DEFAULT_MESSAGES
}


def _humanize_identifier(value: str) -> str:
    words = re.sub(r"[_-]+", " ", str(value or "")).strip()
    return words[:1].upper() + words[1:] if words else "Mensaje"


def _clean_label(label: Optional[str], fallback_key: str) -> str:
    resolved = (label or "").strip() or _humanize_identifier(fallback_key)
    # Private Fase 1 copies carry their option_group after an em dash.  It is useful in
    # storage but not as a business label.
    return re.sub(r"\s+—\s+[a-z0-9_]+$", "", resolved).strip()


def _message_metadata(message_key: str) -> Dict[str, Any]:
    metadata = message_store.get_message_metadata(message_key)
    if metadata is not None:
        return metadata
    default = _DEFAULT_MESSAGE_BY_KEY.get(message_key, {})
    return {
        **default,
        "message_key": message_key,
        "content": default.get("default_content", ""),
    }


def _message_limit(message_key: str, *role_limits: int) -> int:
    metadata = _message_metadata(message_key)
    candidates = [limit for limit in role_limits if limit]
    message_type = metadata.get("message_type", "text")
    declared = message_store.MESSAGE_LIMITS.get(message_type)
    if declared:
        candidates.append(declared)
    return min(candidates) if candidates else message_store.MESSAGE_LIMITS["text"]


def _resolved_message(message_key: str) -> Dict[str, Any]:
    metadata = _message_metadata(message_key)
    default_content = metadata.get("default_content", "")
    return {
        "message_key": message_key,
        "label": _clean_label(metadata.get("label"), message_key),
        "content": message_store.get_message(message_key, default=default_content),
        "limite_caracteres": _message_limit(message_key),
    }


def _group_title(option_group: str) -> str:
    return OPTION_GROUP_META.get(
        option_group,
        (_humanize_identifier(option_group), ""),
    )[0]


def _group_state(option_group: str) -> Optional[str]:
    return OPTION_GROUP_META.get(option_group, ("", None))[1]


def _decode_vars(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _simple_destination(blocks: Dict[str, Dict[str, Any]], block_id: str) -> Dict[str, Any]:
    block = blocks[block_id]
    return {
        "state_id": block.get("_state_id"),
        "label": block["titulo"],
        "block_id": block_id,
    }


def _conditional_destination(
    label: str,
    candidates: Iterable[Dict[str, Any]],
    blocks: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    destinations = []
    for candidate in candidates:
        block_id = candidate["block_id"]
        if block_id not in blocks:
            continue
        destinations.append({
            **candidate,
            "label": blocks[block_id]["titulo"],
        })
    return {
        "es_condicional": True,
        "label": label,
        "destinos_posibles": destinations,
    }


def _declared_destination_from_vars(option: Dict[str, Any]) -> Optional[str]:
    target_vars = _decode_vars(option.get("target_vars"))
    marked_block = target_vars.get("editor_target_block")
    if marked_block in FIXED_BLOCK_SPECS:
        return marked_block
    if option.get("target_state") == "EstadoPedirMail":
        return {
            "registro": "pedido_email_registro",
            "precios": "pedido_email_precios",
        }.get(target_vars.get("flujo"))
    return None


def _option_destination(
    source_block_id: str,
    option: Dict[str, Any],
    blocks: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if (
        source_block_id
        in {
            "borrar_nav_esperando_confirmacion_directa",
            "borrar_nav_esperando_confirmacion_tras_explicar_como",
        }
        and option.get("option_id") == "listo"
    ):
        return _conditional_destination(
            "Depende del recorrido que inició el usuario",
            DELETE_RESULT_DESTINATIONS,
            blocks,
        )

    target_group = option.get("target_option_group")
    if target_group and target_group in blocks:
        return _simple_destination(blocks, target_group)

    declared_block = _declared_destination_from_vars(option)
    if declared_block and declared_block in blocks:
        return _simple_destination(blocks, declared_block)

    reply_key = option.get("reply_key")
    reply_block = REPLY_KEY_BLOCKS.get(reply_key)
    if reply_block and reply_block in blocks:
        return _simple_destination(blocks, reply_block)

    target_state = option.get("target_state")
    canonical = STATE_CANONICAL_BLOCK.get(target_state) if target_state else None
    if canonical and canonical in blocks:
        return _simple_destination(blocks, canonical)

    if option.get("stay_in_state") and source_block_id in blocks:
        return _simple_destination(blocks, source_block_id)

    if target_state:
        return {
            "state_id": target_state,
            "label": FLOW_STATE_LABELS.get(target_state, _humanize_identifier(target_state)),
            "block_id": None,
        }
    return None


def _resolved_option(
    source_block_id: str,
    option: Dict[str, Any],
    blocks: Dict[str, Dict[str, Any]],
    reply_users: Dict[str, List[Tuple[str, str]]],
) -> Dict[str, Any]:
    title_key = option["title_key"]
    button_key = option.get("button_title_key") or title_key
    same_title_key = title_key == button_key
    title_limit = _message_limit(
        title_key,
        message_store.MESSAGE_LIMITS["list_row_title"],
        message_store.MESSAGE_LIMITS["button"] if same_title_key else 0,
    )
    button_limit = _message_limit(
        button_key,
        message_store.MESSAGE_LIMITS["button"],
        message_store.MESSAGE_LIMITS["list_row_title"] if same_title_key else 0,
    )
    description_key = option.get("description_key")
    reply_key = option.get("reply_key")
    response = _resolved_message(reply_key) if reply_key else None
    if response is not None:
        other_reply_users = [
            group_id
            for group_id, option_id in reply_users.get(reply_key, [])
            if (group_id, option_id) != (source_block_id, option["option_id"])
        ]
        other_blocks = sorted(set(other_reply_users), key=_group_title)
        response.update({
            "compartido": bool(other_reply_users),
            "compartido_con": [
                {"block_id": group_id, "titulo": _group_title(group_id)}
                for group_id in other_blocks
            ],
        })
    return {
        "option_id": option["option_id"],
        "orden": option["orden"],
        "editable": True,
        "sintetica": False,
        "titulo": option.get("title", ""),
        "titulo_key": title_key,
        "titulo_limite_caracteres": title_limit,
        "titulo_boton": option.get("button_title", option.get("title", "")),
        "titulo_boton_key": button_key,
        "titulo_boton_limite_caracteres": button_limit,
        "descripcion": option.get("description"),
        "descripcion_key": description_key,
        "descripcion_limite_caracteres": (
            _message_limit(
                description_key,
                message_store.MESSAGE_LIMITS["list_row_description"],
            )
            if description_key
            else message_store.MESSAGE_LIMITS["list_row_description"]
        ),
        "respuesta": response,
        "lleva_a": _option_destination(source_block_id, option, blocks),
    }


def _block_sort_key(block: Dict[str, Any]) -> Tuple[str, int, str]:
    prompt_key = block["prompt"]["message_key"]
    metadata = _message_metadata(prompt_key)
    return (
        FLOW_STATE_LABELS.get(
            block.get("_state_id"),
            block["titulo"],
        ).casefold(),
        metadata.get("orden") if metadata.get("orden") is not None else 999999,
        block["block_id"],
    )


def _neighbor_ids(block: Dict[str, Any]) -> List[str]:
    neighbors: List[str] = []
    for option in sorted(
        block.get("opciones", []),
        key=lambda item: (item.get("orden", 0), item.get("option_id", "")),
    ):
        destination = option.get("lleva_a") or {}
        if destination.get("es_condicional"):
            neighbors.extend(
                candidate["block_id"]
                for candidate in destination.get("destinos_posibles", [])
                if candidate.get("block_id")
            )
        elif destination.get("block_id"):
            neighbors.append(destination["block_id"])
    neighbors.extend(EMAIL_GRAPH_NEIGHBORS.get(block["block_id"], []))
    return neighbors


def _order_blocks(blocks: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    visited = set()
    ordered_ids: List[str] = []
    queue = deque(["menu_principal"] if "menu_principal" in blocks else [])
    while queue:
        block_id = queue.popleft()
        if block_id in visited or block_id not in blocks:
            continue
        visited.add(block_id)
        ordered_ids.append(block_id)
        for neighbor in _neighbor_ids(blocks[block_id]):
            if neighbor not in visited:
                queue.append(neighbor)

    unreachable = sorted(
        (block for block_id, block in blocks.items() if block_id not in visited),
        key=_block_sort_key,
    )
    ordered = [blocks[block_id] for block_id in ordered_ids] + unreachable
    for index, block in enumerate(ordered, start=1):
        block["orden_recorrido"] = index
        block["grupo_recorrido"] = (
            "recorrido_principal" if block["block_id"] in visited else "otros_momentos"
        )
    return ordered


def _strip_internal_fields(block: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in block.items() if not key.startswith("_")}


def build_editor_blocks() -> Dict[str, Any]:
    """Returns the complete, resolved editor projection in deterministic BFS order."""
    groups = message_store.get_option_groups()
    configs = {
        group["option_group"]: message_store.get_option_group_config(group["option_group"])
        for group in groups
    }
    prev_users: Dict[str, List[str]] = defaultdict(list)
    for group_id, config in configs.items():
        for message_key in config.get("prev_message_keys", []):
            prev_users[message_key].append(group_id)

    blocks: Dict[str, Dict[str, Any]] = {}
    raw_options: Dict[str, List[Dict[str, Any]]] = {}
    for group in groups:
        group_id = group["option_group"]
        config = configs[group_id]
        options = message_store.get_option_set(group_id)
        raw_options[group_id] = options
        count = len(options)
        previous_messages = []
        shared_keys = set(config.get("shared_prev_keys", []))
        for message_key in config.get("prev_message_keys", []):
            message = _resolved_message(message_key)
            other_groups = [
                other for other in prev_users.get(message_key, []) if other != group_id
            ]
            message.update({
                "compartido": message_key in shared_keys,
                "compartido_con": [
                    {"block_id": other, "titulo": _group_title(other)}
                    for other in sorted(other_groups, key=_group_title)
                ]
                if message_key in shared_keys
                else [],
            })
            previous_messages.append(message)

        formato = "lista" if count >= 4 else ("botones" if count else "sin_opciones")
        presentation = {
            "texto_boton": config.get("button_text", ""),
            "texto_boton_key": config["button_text_key"],
            "texto_boton_limite_caracteres": _message_limit(
                config["button_text_key"],
                message_store.MESSAGE_LIMITS["button_text"],
            ),
            "titulo_seccion": config.get("section_title", ""),
            "titulo_seccion_key": config["section_title_key"],
            "titulo_seccion_limite_caracteres": _message_limit(
                config["section_title_key"],
                message_store.MESSAGE_LIMITS["list_section_title"],
            ),
        }
        blocks[group_id] = {
            "block_id": group_id,
            "titulo": _group_title(group_id),
            "mensajes_previos": previous_messages,
            "prompt": _resolved_message(config["prompt_key"]),
            "tiene_opciones": count > 0,
            "acepta_nuevas_opciones": count < 10,
            "limites_alta": dict(EDITOR_CREATE_LIMITS),
            "formato": formato,
            "formato_motivo": (
                f"{count} opción" if count == 1 else f"{count} opciones"
            ),
            "presentacion": presentation,
            "opciones": [],
            "_state_id": _group_state(group_id),
            "_option_group": group_id,
        }

    for block_id, spec in FIXED_BLOCK_SPECS.items():
        blocks[block_id] = {
            "block_id": block_id,
            "titulo": spec["titulo"],
            "mensajes_previos": [],
            "prompt": _resolved_message(spec["prompt_key"]),
            "tiene_opciones": False,
            "acepta_nuevas_opciones": False,
            "limites_alta": dict(EDITOR_CREATE_LIMITS),
            "formato": spec["formato"],
            "formato_motivo": spec["formato_motivo"],
            "presentacion": None,
            "opciones": [],
            "_state_id": spec["state_id"],
            "_option_group": None,
        }

    reply_users: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for group_id, options in raw_options.items():
        for option in options:
            if option.get("reply_key"):
                reply_users[option["reply_key"]].append((group_id, option["option_id"]))

    for group_id, options in raw_options.items():
        blocks[group_id]["opciones"] = [
            _resolved_option(group_id, option, blocks, reply_users) for option in options
        ]
    ordered = _order_blocks(blocks)
    public_blocks = [_strip_internal_fields(block) for block in ordered]
    storage = message_store.get_editor_storage_status()
    return {
        "ok": True,
        # El bloque compone mensajes, bindings y configuración. Solo se declara
        # BigQuery cuando los tres catálogos provienen íntegramente de allí.
        "source": storage["source"],
        "blocks": public_blocks,
        "count": len(public_blocks),
        "storage": storage,
    }


def _find_block(block_id: str) -> Dict[str, Any]:
    payload = build_editor_blocks()
    block = next((item for item in payload["blocks"] if item["block_id"] == block_id), None)
    if block is None:
        raise EditorBlockNotFound(f"block_id '{block_id}' no existe", field="block_id")
    return block


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    return normalized[:48] or "opcion"


def _unique_option_id(option_group: str, title: str) -> str:
    existing_ids = {option["option_id"] for option in message_store.get_option_set(option_group)}
    base = _slug(title)
    candidate = base
    suffix = 2
    while candidate in existing_ids or message_store.get_message_metadata(
        f"{option_group}_{candidate}_title"
    ):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _resolve_post_destination(raw: Any) -> Dict[str, Any]:
    current_group_ids = {
        group["option_group"] for group in message_store.get_option_groups()
    }
    if isinstance(raw, str):
        state_id = raw.strip()
        if state_id not in FLOW_STATE_LABELS:
            raise EditorBlockError(
                f"lleva_a '{state_id}' no es un estado válido", field="lleva_a"
            )
        block_id = STATE_CANONICAL_BLOCK.get(state_id)
        if not block_id:
            raise EditorBlockError(
                f"el estado '{state_id}' tiene más de un bloque de entrada; indicá block_id",
                field="lleva_a",
            )
    elif isinstance(raw, dict):
        state_id = str(raw.get("state_id") or "").strip()
        block_id = str(raw.get("block_id") or "").strip()
        if state_id not in FLOW_STATE_LABELS:
            raise EditorBlockError(f"state_id '{state_id}' no es válido", field="lleva_a")
        if not block_id:
            raise EditorBlockError("lleva_a.block_id es obligatorio", field="lleva_a")
    else:
        raise EditorBlockError(
            "lleva_a debe ser un state_id o un objeto con state_id/block_id", field="lleva_a"
        )

    payload = build_editor_blocks()
    target = next(
        (block for block in payload["blocks"] if block["block_id"] == block_id),
        None,
    )
    if target is None:
        raise EditorBlockError(f"lleva_a.block_id '{block_id}' no existe", field="lleva_a")
    expected_state = (
        OPTION_GROUP_META.get(block_id, (None, None))[1]
        or FIXED_BLOCK_SPECS.get(block_id, {}).get("state_id")
    )
    if expected_state and expected_state != state_id:
        raise EditorBlockError(
            f"el bloque '{block_id}' pertenece a {expected_state}, no a {state_id}",
            field="lleva_a",
        )
    return {
        "state_id": state_id,
        "block_id": block_id,
        "target_option_group": block_id if block_id in current_group_ids else None,
        "target_vars": BLOCK_TARGET_VARS.get(block_id),
    }


def _validate_content(value: Any, field_name: str, limit: int, *, required: bool) -> Optional[str]:
    text = str(value or "").strip()
    if required and not text:
        raise EditorBlockError(f"{field_name} es obligatorio", field=field_name)
    if len(text) > limit:
        raise EditorBlockError(f"{field_name} excede {limit} caracteres", field=field_name)
    return text or None


def _promote_existing_buttons_to_list(
    option_group: str,
    options: List[Dict[str, Any]],
    *,
    source_state: Optional[str],
    updated_by: str,
) -> None:
    """Give every existing button an independent list title without changing its text."""

    for option in options:
        current_title_key = option["title_key"]
        current_button_key = option.get("button_title_key") or current_title_key
        if current_title_key != current_button_key:
            continue

        list_title_key = f"{option_group}_{option['option_id']}_list_title"
        if message_store.get_message_metadata(list_title_key) is None:
            button_text = option.get("button_title") or option.get("title") or ""
            message_store.create_message(
                list_title_key,
                "list_row_title",
                source_state,
                f"Título de lista: {button_text}",
                button_text,
                updated_by=updated_by,
            )
        message_store.update_option_binding(
            option_group,
            option["option_id"],
            title_key=list_title_key,
            button_title_key=current_button_key,
            updated_by=updated_by,
        )


def _order_for_position(options: List[Dict[str, Any]], position: int) -> int:
    ordered = sorted(options, key=lambda option: (option["orden"], option["option_id"]))
    if not ordered:
        return 10
    if position == len(ordered) + 1:
        return max(option["orden"] for option in ordered) + 10
    if position == 1:
        first = ordered[0]["orden"]
        if first >= 2:
            return first // 2
    else:
        previous = ordered[position - 2]["orden"]
        following = ordered[position - 1]["orden"]
        if following - previous >= 2:
            return previous + ((following - previous) // 2)

    # Fine-grained edits may consume all integer gaps.  Normalize the existing rows and
    # preserve their relative order before assigning the requested slot.
    for index, option in enumerate(ordered, start=1):
        normalized_order = index * 20
        if option["orden"] != normalized_order:
            message_store.update_option_binding(
                option["option_group"],
                option["option_id"],
                orden=normalized_order,
                updated_by="system-editor-reorder",
            )
    return (position * 20) - 10


def _validate_response_request(response: Any) -> Tuple[str, Optional[str]]:
    if not isinstance(response, dict):
        raise EditorBlockError("respuesta debe ser un objeto", field="respuesta")
    mode = str(response.get("modo") or "").strip()
    if mode not in {"nuevo", "sin_respuesta"}:
        raise EditorBlockError(
            "respuesta.modo debe ser 'nuevo' o 'sin_respuesta'",
            field="respuesta.modo",
        )
    if mode == "nuevo":
        content = _validate_content(
            response.get("contenido"),
            "respuesta.contenido",
            message_store.MESSAGE_LIMITS["text"],
            required=True,
        )
        return mode, content
    return mode, None


def _message_model_references(message_key: str) -> List[Tuple[str, str]]:
    """Find active model references; runtime-only retry texts are intentionally not owners."""

    references: List[Tuple[str, str]] = []
    for group in message_store.get_option_groups():
        group_id = group["option_group"]
        config = message_store.get_option_group_config(group_id)
        for field in ("button_text_key", "section_title_key", "prompt_key"):
            if config.get(field) == message_key:
                references.append((group_id, field))
        for field in ("prev_message_keys", "shared_prev_keys"):
            if message_key in config.get(field, []):
                references.append((group_id, field))
        for option in message_store.get_option_set(group_id):
            for field in ("title_key", "button_title_key", "description_key", "reply_key"):
                if option.get(field) == message_key:
                    references.append((group_id, f"{option['option_id']}.{field}"))
    return references


def _discard_detached_editor_reply(message_key: Optional[str], updated_by: str) -> None:
    if not message_key or _message_model_references(message_key):
        return
    metadata = message_store.get_message_metadata(message_key)
    if metadata is None:
        return
    # Solo los mensajes creados por el editor son propiedad de una opción. Un mensaje existente
    # puede ser un prompt o retry usado directamente por el runtime aunque no figure en bindings.
    if not str(metadata.get("label") or "").startswith("Respuesta al elegir:"):
        return
    message_store.delete_message(message_key, updated_by=updated_by)


def create_composed_option(block_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Create the messages and binding for one business-facing option in one request."""
    source_block = _find_block(block_id)
    if not source_block.get("acepta_nuevas_opciones"):
        raise EditorBlockConflict(f"el bloque '{block_id}' no acepta nuevas opciones")
    if block_id not in {group["option_group"] for group in message_store.get_option_groups()}:
        raise EditorBlockConflict(f"el bloque '{block_id}' no tiene option_group")

    current = message_store.get_option_set(block_id)
    if len(current) >= 10:
        raise EditorBlockConflict("un bloque no puede superar 10 opciones")
    position = request.get("posicion")
    if not isinstance(position, int) or isinstance(position, bool):
        raise EditorBlockError("posicion debe ser un entero", field="posicion")
    if position < 1 or position > len(current) + 1:
        raise EditorBlockError(
            f"posicion debe estar entre 1 y {len(current) + 1}", field="posicion"
        )
    if len(current) == 3 and not request.get("permitir_cambio_a_lista", False):
        raise EditorBlockConflict(
            "la cuarta opción cambia el formato a lista; enviá permitir_cambio_a_lista=true"
        )

    resulting_format = "lista" if len(current) + 1 >= 4 else "botones"
    title_type = "list_row_title" if resulting_format == "lista" else "button"
    title = _validate_content(
        request.get("titulo"),
        "titulo",
        message_store.MESSAGE_LIMITS[title_type],
        required=True,
    )
    description = _validate_content(
        request.get("descripcion"),
        "descripcion",
        message_store.MESSAGE_LIMITS["list_row_description"],
        required=resulting_format == "lista",
    )
    button_title = (
        _validate_content(
            request.get("titulo_boton"),
            "titulo_boton",
            message_store.MESSAGE_LIMITS["button"],
            required=True,
        )
        if resulting_format == "lista"
        else title
    )
    mode, response_value = _validate_response_request(request.get("respuesta"))

    destination = _resolve_post_destination(request.get("lleva_a"))
    option_id = _unique_option_id(block_id, title or "opcion")
    title_key = f"{block_id}_{option_id}_title"
    button_title_key = (
        f"{block_id}_{option_id}_button_title"
        if resulting_format == "lista"
        else title_key
    )
    description_key = f"{block_id}_{option_id}_description" if description else None
    updated_by = str(request.get("updated_by") or "").strip()
    source_state = OPTION_GROUP_META.get(block_id, (None, None))[1]

    reply_key: Optional[str] = None
    reply_content: Optional[str] = None
    if mode == "nuevo":
        reply_content = response_value
        reply_key = f"{block_id}_{option_id}_reply_text"
        if message_store.get_message_metadata(reply_key) is not None:
            raise EditorBlockConflict(f"message_key '{reply_key}' ya existe")
    order = _order_for_position(current, position)
    if len(current) == 3:
        _promote_existing_buttons_to_list(
            block_id,
            current,
            source_state=source_state,
            updated_by=updated_by,
        )
    # These calls intentionally use the existing stores.  Their versioning, validation and
    # cache invalidation remain the single source of truth for persistence.
    message_store.create_message(
        title_key,
        title_type,
        source_state,
        f"Opción: {title}",
        title or "",
        updated_by=updated_by,
    )
    if button_title_key != title_key:
        message_store.create_message(
            button_title_key,
            "button",
            source_state,
            f"Texto de botón de la opción: {title}",
            button_title or "",
            updated_by=updated_by,
        )
    if description_key and description:
        message_store.create_message(
            description_key,
            "list_row_description",
            source_state,
            f"Descripción de la opción: {title}",
            description,
            updated_by=updated_by,
        )
    if mode == "nuevo" and reply_content:
        message_store.create_message(
            reply_key,
            "text",
            destination["state_id"],
            f"Respuesta al elegir: {title}",
            reply_content,
            updated_by=updated_by,
        )

    message_store.create_option_binding(
        block_id,
        option_id,
        order,
        title_key,
        button_title_key=button_title_key,
        description_key=description_key,
        target_state=destination["state_id"],
        target_vars=destination["target_vars"],
        target_option_group=destination["target_option_group"],
        reply_key=reply_key,
        updated_by=updated_by,
    )
    message_store.invalidate_cache()
    return _find_block(block_id)


def set_option_response(
    block_id: str,
    option_id: str,
    request: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach, replace or remove the reply owned by one option."""

    _find_block(block_id)
    option = message_store.get_option_by_id(block_id, option_id)
    if option is None:
        raise EditorBlockNotFound(
            f"la opción solicitada no existe en '{block_id}'", field="option_id"
        )

    mode, response_value = _validate_response_request(request.get("respuesta"))
    updated_by = str(request.get("updated_by") or "").strip()
    previous_reply_key = option.get("reply_key")
    next_reply_key: Optional[str] = None

    if mode == "nuevo":
        next_reply_key = (
            f"{block_id}_{option_id}_reply_{uuid.uuid4().hex[:12]}"
        )
        message_store.create_message(
            next_reply_key,
            "text",
            option.get("target_state"),
            f"Respuesta al elegir: {option.get('title') or option.get('button_title')}",
            response_value or "",
            updated_by=updated_by,
        )
    message_store.update_option_binding(
        block_id,
        option_id,
        reply_key=next_reply_key,
        updated_by=updated_by,
    )
    if previous_reply_key != next_reply_key:
        _discard_detached_editor_reply(previous_reply_key, updated_by)
    message_store.invalidate_cache()
    return _find_block(block_id)


__all__ = [
    "EditorBlockError",
    "EditorBlockNotFound",
    "EditorBlockConflict",
    "build_editor_blocks",
    "create_composed_option",
    "set_option_response",
]
