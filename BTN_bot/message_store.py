"""
message_store.py - Cache en memoria de mensajes editables del bot.

Capa intermedia entre bot.py y la tabla BigQuery `bot_messages`. Carga todos
los mensajes en memoria con un TTL de 5 minutos para que el bot no haga una
query a BigQuery por cada mensaje en cada conversación.

Interfaz pública:
    get_message(key, default="") -> str
    set_message(key, content, updated_by="") -> None
    load_all_messages() -> Dict[str, str]
    invalidate_cache() -> None
    get_all_messages_with_metadata() -> List[Dict[str, Any]]
"""

import os
import time
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None

logger = logging.getLogger(__name__)

# ==============================
# Configuración
# ==============================

BIGQUERY_DATASET = os.getenv("BIGQUERY_CHAT_LOG_DATASET", "inspectia_logs")
BIGQUERY_BOT_MESSAGES_TABLE = os.getenv("BIGQUERY_BOT_MESSAGES_TABLE", "bot_messages")
CACHE_TTL_SECONDS = 5 * 60

MESSAGE_LIMITS: Dict[str, int] = {
    "button": 20,
    "list_row_title": 24,
    "list_row_description": 72,
    "text": 2000,
    "button_text": 2000,
}

# ==============================
# Catálogo de defaults (extraído de flow_states_btn.py)
# ==============================

DEFAULT_MESSAGES: List[Dict[str, Any]] = [
    {
        "message_key": "welcome_message",
        "message_type": "text",
        "state_name": None,
        "label": "Mensaje de bienvenida",
        "default_content": (
            "Hola, gracias por comunicarte con la Plataforma de Beneficios de Motorola.\n"
            "¿En qué te puedo ayudar hoy?"
        ),
    },
    {
        "message_key": "main_menu_button_text",
        "message_type": "button_text",
        "state_name": "EstadoInicial",
        "label": "Texto del botón del menú principal",
        "default_content": "Ver opciones",
    },
    {
        "message_key": "main_menu_row_registro_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "label": "Título - No me puedo registrar",
        "default_content": "No me puedo registrar",
    },
    {
        "message_key": "main_menu_row_registro_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "label": "Descripción - No me puedo registrar",
        "default_content": "Problemas con el registro",
    },
    {
        "message_key": "main_menu_row_no_veo_precios_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "label": "Título - No veo precios",
        "default_content": "No veo precios",
    },
    {
        "message_key": "main_menu_row_no_veo_precios_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "label": "Descripción - No veo precios",
        "default_content": "No se muestran los precios",
    },
    {
        "message_key": "main_menu_row_no_veo_descuentos_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "label": "Título - No veo descuentos",
        "default_content": "No veo descuentos",
    },
    {
        "message_key": "main_menu_row_no_veo_descuentos_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "label": "Descripción - No veo descuentos",
        "default_content": "Descuentos no aplicados",
    },
    {
        "message_key": "main_menu_row_info_pedido_title",
        "message_type": "list_row_title",
        "state_name": "EstadoInicial",
        "label": "Título - Info de mi pedido",
        "default_content": "Info de mi pedido",
    },
    {
        "message_key": "main_menu_row_info_pedido_description",
        "message_type": "list_row_description",
        "state_name": "EstadoInicial",
        "label": "Descripción - Info de mi pedido",
        "default_content": "Consultar estado del pedido",
    },
    {
        "message_key": "pre_flujo_message",
        "message_type": "text",
        "state_name": "EstadoPreFlujo",
        "label": "Mensaje de empatía pre-flujo",
        "default_content": (
            "Entiendo que acceder a la Plataforma puede ser complicado, "
            "pero te aseguramos que vas a tener buenos beneficios. "
            "Mientras esperas que te atendamos, podés ir probando estos pasos."
        ),
    },
]

# ==============================
# Cliente BigQuery
# ==============================

_BIGQUERY_CLIENT = None
_CLIENT_LOCK = Lock()


def _bigquery_configured() -> bool:
    return bigquery is not None and bool(os.getenv("GOOGLE_CLOUD_PROJECT"))


def _table_id(client) -> str:
    return f"{client.project}.{BIGQUERY_DATASET}.{BIGQUERY_BOT_MESSAGES_TABLE}"


def _ensure_table(client) -> None:
    dataset_id = f"{client.project}.{BIGQUERY_DATASET}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    client.create_dataset(dataset, exists_ok=True)

    schema = [
        bigquery.SchemaField("message_key", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("message_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("state_name", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("label", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("default_content", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("updated_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("updated_by", "STRING", mode="NULLABLE"),
    ]
    table = bigquery.Table(f"{dataset_id}.{BIGQUERY_BOT_MESSAGES_TABLE}", schema=schema)
    client.create_table(table, exists_ok=True)

    _seed_if_empty(client)


def _seed_if_empty(client) -> None:
    table_id = _table_id(client)
    rows = list(client.query(f"SELECT COUNT(*) AS c FROM `{table_id}`").result())
    if rows and rows[0]["c"]:
        return

    now = datetime.now(timezone.utc).isoformat()
    seed_rows = [
        {
            "message_key": m["message_key"],
            "message_type": m["message_type"],
            "state_name": m["state_name"],
            "label": m["label"],
            "content": m["default_content"],
            "default_content": m["default_content"],
            "updated_at": now,
            "updated_by": "system-seed",
        }
        for m in DEFAULT_MESSAGES
    ]
    errors = client.insert_rows_json(table_id, seed_rows)
    if errors:
        logger.error(f"[message-store] error pre-poblando bot_messages: {errors}")
    else:
        logger.info(f"[message-store] tabla bot_messages pre-poblada con {len(seed_rows)} defaults")


def _get_client():
    global _BIGQUERY_CLIENT

    if not _bigquery_configured():
        raise RuntimeError(
            "BigQuery no está configurado (falta google-cloud-bigquery o GOOGLE_CLOUD_PROJECT)"
        )

    with _CLIENT_LOCK:
        if _BIGQUERY_CLIENT is None:
            client = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
            _ensure_table(client)
            _BIGQUERY_CLIENT = client
        return _BIGQUERY_CLIENT


def _load_from_bigquery() -> List[Dict[str, Any]]:
    client = _get_client()
    query = f"""
        SELECT message_key, message_type, state_name, label, content, default_content, updated_at, updated_by
        FROM `{_table_id(client)}`
        QUALIFY ROW_NUMBER() OVER (PARTITION BY message_key ORDER BY updated_at DESC) = 1
    """
    rows = client.query(query).result()
    records = []
    for row in rows:
        records.append({
            "message_key": row["message_key"],
            "message_type": row["message_type"],
            "state_name": row["state_name"],
            "label": row["label"],
            "content": row["content"],
            "default_content": row["default_content"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "updated_by": row["updated_by"],
        })
    return records


# ==============================
# Cache en memoria
# ==============================

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = Lock()
_CACHE_LOADED_AT: Optional[float] = None


def _default_records() -> List[Dict[str, Any]]:
    return [
        {
            "message_key": m["message_key"],
            "message_type": m["message_type"],
            "state_name": m["state_name"],
            "label": m["label"],
            "content": m["default_content"],
            "default_content": m["default_content"],
            "updated_at": None,
            "updated_by": None,
        }
        for m in DEFAULT_MESSAGES
    ]


def _populate_cache() -> None:
    global _CACHE_LOADED_AT

    records: Optional[List[Dict[str, Any]]] = None
    source = "default"

    if _bigquery_configured():
        try:
            records = _load_from_bigquery()
            source = "bigquery"
        except Exception as e:
            logger.warning(f"[message-store] BigQuery no disponible, usando defaults: {e}")
            records = None

    if records is None:
        records = _default_records()
        source = "default"

    with _CACHE_LOCK:
        _CACHE.clear()
        for record in records:
            entry = dict(record)
            entry["source"] = source
            _CACHE[entry["message_key"]] = entry
        _CACHE_LOADED_AT = time.monotonic()

    logger.info(f"[message-store] cache cargado con {len(records)} mensajes")


def _ensure_cache_fresh() -> None:
    with _CACHE_LOCK:
        loaded_at = _CACHE_LOADED_AT
        is_empty = not _CACHE

    needs_reload = is_empty or loaded_at is None or (time.monotonic() - loaded_at) > CACHE_TTL_SECONDS
    if needs_reload:
        _populate_cache()


# ==============================
# Interfaz pública
# ==============================

def get_message(key: str, default: str = "") -> str:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    return entry["content"] if entry else default


def get_message_metadata(key: str) -> Optional[Dict[str, Any]]:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    return dict(entry) if entry else None


def load_all_messages() -> Dict[str, str]:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        return {key: entry["content"] for key, entry in _CACHE.items()}


def get_all_messages_with_metadata() -> List[Dict[str, Any]]:
    _ensure_cache_fresh()
    with _CACHE_LOCK:
        items = [dict(entry) for entry in _CACHE.values()]
    items.sort(key=lambda m: (m.get("state_name") is not None, m.get("state_name") or "", m["message_key"]))
    return items


def set_message(key: str, content: str, updated_by: str = "") -> None:
    meta = get_message_metadata(key)
    if meta is None:
        raise KeyError(key)

    client = _get_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "message_key": key,
        "message_type": meta["message_type"],
        "state_name": meta.get("state_name"),
        "label": meta.get("label"),
        "content": content,
        "default_content": meta["default_content"],
        "updated_at": now,
        "updated_by": updated_by or "",
    }
    errors = client.insert_rows_json(_table_id(client), [row])
    if errors:
        raise RuntimeError(str(errors))

    invalidate_cache()


def invalidate_cache() -> None:
    global _CACHE_LOADED_AT
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_LOADED_AT = None
    logger.info("[message-store] cache invalidado")
