"""Carga una conversación visual de ejemplo exclusivamente en el log local.

Uso desde ``BTN_bot``::

    python dev_tools/load_sample_chat_turns.py --confirm-local

La herramienta no se importa desde ``bot.py`` y se niega a ejecutar si detecta BigQuery,
Cloud Run o un entorno declarado productivo.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

BTN_BOT_DIR = Path(__file__).resolve().parents[1]
if str(BTN_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BTN_BOT_DIR))

import chat_turn_store


def assert_local_execution() -> None:
    reasons = []
    if os.getenv("GOOGLE_CLOUD_PROJECT", "").strip():
        reasons.append("GOOGLE_CLOUD_PROJECT está configurado")
    if os.getenv("BIGQUERY_CHAT_LOG_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        reasons.append("BigQuery está habilitado")
    if os.getenv("CLOUD_RUN", "").strip().lower() == "true" or os.getenv("K_SERVICE", "").strip():
        reasons.append("se detectó Cloud Run")
    if os.getenv("APP_ENV", "").strip().lower() in {"production", "prod"}:
        reasons.append("APP_ENV es productivo")
    if reasons:
        raise RuntimeError(
            "El cargador de ejemplo solo puede ejecutarse en desarrollo local: "
            + ", ".join(reasons)
        )

    resolved_log = chat_turn_store.CHAT_TURNS_PATH.resolve()
    expected_logs_dir = (chat_turn_store.BASE_DIR / "logs").resolve()
    if expected_logs_dir not in resolved_log.parents:
        raise RuntimeError("El destino de chat_turns no está dentro del directorio local de logs")


def build_sample_turns(
    *,
    session_id: str,
    now: datetime | None = None,
) -> List[Dict[str, Any]]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = current - timedelta(hours=50)
    common = {
        "session_id": session_id,
        "channel": "whatsapp",
        "environment": "local",
    }

    def turn(offset_minutes: int, **values: Any) -> Dict[str, Any]:
        return {
            **common,
            "timestamp": (start + timedelta(minutes=offset_minutes)).isoformat(),
            **values,
        }

    turns = [
        turn(
            0,
            tipo="mensaje",
            autor="bot",
            texto="Hola, ¿en qué te podemos ayudar hoy?",
        ),
        turn(
            1,
            tipo="botones",
            autor="bot",
            texto="Elegí una de estas opciones.",
            interactive_type="button",
            opciones=[
                {"id": "precios", "title": "Consultar precios"},
                {"id": "registro", "title": "Registrarme"},
                {"id": "ayuda", "title": "Necesito ayuda"},
            ],
        ),
        turn(
            2,
            tipo="respuesta_usuario",
            autor="usuario",
            texto="Consultar precios",
            seleccion_opcion=True,
            opcion_id_seleccionada="precios",
        ),
        turn(
            3,
            tipo="lista",
            autor="bot",
            texto="¿Cuándo completaste el formulario?",
            nombre_lista="Ver opciones",
            interactive_type="list",
            opciones=[
                {"id": "menos_24", "title": "Hace menos de 24 h", "description": "Todavía está en proceso"},
                {"id": "entre_24_48", "title": "Entre 24 y 48 h", "description": "Revisamos el estado"},
                {"id": "mas_48", "title": "Hace más de 48 h", "description": "Te ayudamos a resolverlo"},
                {"id": "no_recuerdo", "title": "No lo recuerdo", "description": "Podés continuar igual"},
            ],
        ),
        turn(
            4,
            tipo="respuesta_usuario",
            autor="usuario",
            texto="Lo completé la semana pasada, pero no recuerdo el día.",
            seleccion_opcion=False,
        ),
        turn(
            5,
            tipo="sistema",
            autor="sistema",
            texto="La conversación pasó a espera de atención.",
            status="handoff_waiting",
        ),
        turn(
            6,
            tipo="sistema",
            autor="sistema",
            texto="Hola, ya estoy revisando tu caso.",
            status="agente",
        ),
        turn(
            7,
            tipo="sistema",
            autor="sistema",
            texto="Te enviamos el enlace con los pasos para continuar.",
            status="bot_manual",
        ),
    ]
    turns.append({
        **common,
        "timestamp": (start + timedelta(hours=26)).isoformat(),
        "tipo": "mensaje",
        "autor": "bot",
        "texto": "Retomamos esta conversación después de más de 24 horas.",
    })
    return turns


def load_sample_conversation(*, now: datetime | None = None) -> Dict[str, Any]:
    assert_local_execution()
    session_id = f"dev:visor-ejemplo:{uuid.uuid4().hex[:8]}"
    saved = []
    for turn in build_sample_turns(session_id=session_id, now=now):
        record = chat_turn_store.append_turn(turn)
        if record is None:
            raise RuntimeError("No se pudo escribir uno de los turnos de ejemplo")
        saved.append(record)
    return {
        "session_id": session_id,
        "count": len(saved),
        "new_sessions": sum(1 for item in saved if item.get("nueva_sesion")),
        "path": str(chat_turn_store.CHAT_TURNS_PATH),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Carga turnos de ejemplo únicamente en el archivo local de desarrollo."
    )
    parser.add_argument(
        "--confirm-local",
        action="store_true",
        help="Confirma que se quiere escribir en el log local de desarrollo.",
    )
    args = parser.parse_args()
    if not args.confirm_local:
        parser.error("falta --confirm-local; no se modificó ningún archivo")

    result = load_sample_conversation()
    print(f"Conversación de ejemplo creada: {result['count']} turnos")
    print(f"Separadores de sesión esperados: {result['new_sessions']}")
    print(f"Archivo local: {result['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
