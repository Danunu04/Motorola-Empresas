"""Persistencia tolerante a fallos de conversaciones por turnos atómicos.

`chat_history` sigue siendo responsabilidad de ``bot.py`` durante la transición. Este
módulo administra exclusivamente ``chat_turns`` y mantiene toda operación de logging
fuera del camino crítico de la conversación.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

try:
    from google.cloud import bigquery
except ImportError:  # pragma: no cover - cubierto en instalaciones sin el extra.
    bigquery = None


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CHAT_TURNS_PATH = BASE_DIR / "logs" / "chat_turns.json"
# Sin default productivo: si se usa GCP, el dataset debe declararse explícitamente.
BIGQUERY_DATASET = os.getenv("BIGQUERY_CHAT_LOG_DATASET", "").strip()
# Nombre lógico estable dentro del dataset explícito; no introduce riesgo de cruce.
BIGQUERY_TABLE = os.getenv("BIGQUERY_CHAT_TURNS_TABLE", "chat_turns")
BIGQUERY_ENABLED = os.getenv("BIGQUERY_CHAT_LOG_ENABLED", "").lower() in {
    "1",
    "true",
    "yes",
}
SESSION_CUTOFF = timedelta(hours=24)
BIGQUERY_DATASET_REQUIRED_MESSAGE = (
    "BIGQUERY_CHAT_LOG_DATASET no está definida — configurá el dataset explícitamente"
)

CHAT_TURN_FIELDS = [
    "turn_id",
    "session_id",
    "timestamp",
    "fecha",
    "orden",
    "tipo",
    "autor",
    "texto",
    "opciones",
    "nombre_lista",
    "interactive_type",
    "seleccion_opcion",
    "opcion_id_seleccionada",
    "nueva_sesion",
    "status",
    "channel",
    "environment",
]

_CLIENT = None
_CLIENT_LOCK = Lock()
_TURN_LOCK = Lock()
_LAST_TURN_CACHE: Dict[str, Tuple[datetime, int]] = {}


def _uses_bigquery() -> bool:
    return (
        BIGQUERY_ENABLED
        and bigquery is not None
        and bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())
    )


def _require_bigquery_dataset() -> str:
    if not BIGQUERY_DATASET:
        raise RuntimeError(BIGQUERY_DATASET_REQUIRED_MESSAGE)
    return BIGQUERY_DATASET


def _table_id(client=None) -> str:
    project = (
        getattr(client, "project", None)
        or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    )
    return f"{project}.{_require_bigquery_dataset()}.{BIGQUERY_TABLE}"


def _schema():
    return [
        bigquery.SchemaField("turn_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("fecha", "DATE", mode="NULLABLE"),
        bigquery.SchemaField("orden", "INTEGER", mode="REQUIRED"),
        bigquery.SchemaField("tipo", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("autor", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("texto", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("opciones", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("nombre_lista", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("interactive_type", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("seleccion_opcion", "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("opcion_id_seleccionada", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("nueva_sesion", "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("status", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("channel", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("environment", "STRING", mode="NULLABLE"),
    ]


def _ensure_table(client) -> None:
    dataset_id = f"{client.project}.{_require_bigquery_dataset()}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    client.create_dataset(dataset, exists_ok=True)

    table = bigquery.Table(_table_id(client), schema=_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="fecha",
    )
    table.clustering_fields = ["session_id", "orden"]
    client.create_table(table, exists_ok=True)


def _get_client():
    global _CLIENT

    if bigquery is None:
        raise RuntimeError("google-cloud-bigquery no está instalado")

    with _CLIENT_LOCK:
        if _CLIENT is None:
            candidate = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
            # Solo se publica el cliente si la creación/verificación terminó bien.
            _ensure_table(candidate)
            _CLIENT = candidate
        return _CLIENT


def _as_utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def _load_local_records() -> List[Dict[str, Any]]:
    if not CHAT_TURNS_PATH.exists():
        return []
    try:
        with open(CHAT_TURNS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_local_records(records: List[Dict[str, Any]]) -> None:
    CHAT_TURNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CHAT_TURNS_PATH.with_suffix(f"{CHAT_TURNS_PATH.suffix}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, CHAT_TURNS_PATH)


def _hydrate_local_cache() -> None:
    latest: Dict[str, Tuple[datetime, int]] = {}
    for record in _load_local_records():
        session_id = str(record.get("session_id") or "")
        if not session_id:
            continue
        timestamp = _as_utc_datetime(record.get("timestamp"))
        order = int(record.get("orden") or 0)
        previous = latest.get(session_id)
        if previous is None or order > previous[1]:
            latest[session_id] = (timestamp, order)
    _LAST_TURN_CACHE.update(latest)


def _hydrate_bigquery_cache(client) -> None:
    query = f"""
        SELECT session_id, timestamp, orden
        FROM `{_table_id(client)}`
        WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY session_id ORDER BY orden DESC, timestamp DESC
        ) = 1
    """
    rows = client.query(query).result()
    for row in rows:
        session_id = str(_row_value(row, "session_id") or "")
        if not session_id:
            continue
        _LAST_TURN_CACHE[session_id] = (
            _as_utc_datetime(_row_value(row, "timestamp")),
            int(_row_value(row, "orden", 0) or 0),
        )


def initialize() -> bool:
    """Crea la tabla e hidrata caché sin propagar ningún error al startup."""

    try:
        with _TURN_LOCK:
            if _uses_bigquery():
                client = _get_client()
                _hydrate_bigquery_cache(client)
            else:
                _hydrate_local_cache()
        return True
    except Exception as exc:  # El log nunca puede impedir que el bot arranque.
        logger.error("[chat-turns] no se pudo inicializar: %s", exc, exc_info=True)
        return False


def _last_turn_bigquery(client, session_id: str) -> Optional[Tuple[datetime, int]]:
    query = f"""
        SELECT timestamp, orden
        FROM `{_table_id(client)}`
        WHERE session_id = @session_id
        ORDER BY orden DESC, timestamp DESC
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("session_id", "STRING", session_id),
        ]
    )
    rows = list(client.query(query, job_config=job_config).result())
    if not rows:
        return None
    return (
        _as_utc_datetime(_row_value(rows[0], "timestamp")),
        int(_row_value(rows[0], "orden", 0) or 0),
    )


def _last_turn_local(session_id: str) -> Optional[Tuple[datetime, int]]:
    latest: Optional[Tuple[datetime, int]] = None
    for record in _load_local_records():
        if str(record.get("session_id") or "") != session_id:
            continue
        candidate = (
            _as_utc_datetime(record.get("timestamp")),
            int(record.get("orden") or 0),
        )
        if latest is None or candidate[1] > latest[1]:
            latest = candidate
    return latest


def _last_turn(session_id: str, client=None) -> Optional[Tuple[datetime, int]]:
    if session_id in _LAST_TURN_CACHE:
        return _LAST_TURN_CACHE[session_id]
    previous = (
        _last_turn_bigquery(client, session_id)
        if client is not None
        else _last_turn_local(session_id)
    )
    if previous is not None:
        _LAST_TURN_CACHE[session_id] = previous
    return previous


def _serialize_options(value: Any) -> Optional[str]:
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, list):
        raise ValueError("opciones debe ser un array")
    normalized = []
    for option in decoded:
        if not isinstance(option, dict):
            continue
        item = {
            "id": str(option.get("id") or ""),
            "title": str(option.get("title") or ""),
        }
        if option.get("description") not in (None, ""):
            item["description"] = str(option["description"])
        normalized.append(item)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _normalize_turn(
    turn: Dict[str, Any],
    previous: Optional[Tuple[datetime, int]],
) -> Dict[str, Any]:
    session_id = str(turn.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id es obligatorio")

    timestamp = _as_utc_datetime(turn.get("timestamp"))
    order = previous[1] + 1 if previous is not None else 0
    new_session = previous is None or (timestamp - previous[0]) > SESSION_CUTOFF
    author = str(turn.get("autor") or "").strip()
    turn_type = str(turn.get("tipo") or "").strip()
    if author not in {"bot", "usuario", "sistema"}:
        raise ValueError("autor debe ser bot, usuario o sistema")
    if turn_type not in {"mensaje", "lista", "botones", "respuesta_usuario", "sistema"}:
        raise ValueError("tipo de turno inválido")

    selection = turn.get("seleccion_opcion")
    if author == "usuario":
        selection = bool(selection)
    else:
        selection = None

    return {
        "turn_id": str(turn.get("turn_id") or uuid.uuid4()),
        "session_id": session_id,
        "timestamp": timestamp.isoformat(),
        "fecha": timestamp.date().isoformat(),
        "orden": order,
        "tipo": turn_type,
        "autor": author,
        "texto": str(turn.get("texto") or ""),
        "opciones": _serialize_options(turn.get("opciones")),
        "nombre_lista": turn.get("nombre_lista") or None,
        "interactive_type": turn.get("interactive_type") or None,
        "seleccion_opcion": selection,
        "opcion_id_seleccionada": turn.get("opcion_id_seleccionada") or None,
        "nueva_sesion": new_session,
        "status": turn.get("status") or None,
        "channel": turn.get("channel")
        or ("whatsapp" if session_id.startswith("wa:") else "api"),
        "environment": turn.get("environment")
        or ("cloud_run" if os.getenv("CLOUD_RUN") == "true" else "local"),
    }


def _append_bigquery(client, record: Dict[str, Any]) -> None:
    errors = client.insert_rows_json(
        _table_id(client),
        [record],
        row_ids=[record["turn_id"]],
    )
    if errors:
        raise RuntimeError(str(errors))


def append_turn(turn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Persiste un turno y devuelve el registro, o ``None`` ante cualquier fallo."""

    try:
        with _TURN_LOCK:
            client = _get_client() if _uses_bigquery() else None
            session_id = str(turn.get("session_id") or "").strip()
            previous = _last_turn(session_id, client=client)
            record = _normalize_turn(turn, previous)
            if client is not None:
                _append_bigquery(client, record)
            else:
                records = _load_local_records()
                records.append(record)
                _write_local_records(records)
            _LAST_TURN_CACHE[record["session_id"]] = (
                _as_utc_datetime(record["timestamp"]),
                int(record["orden"]),
            )
            return dict(record)
    except Exception as exc:  # El log nunca puede interrumpir una conversación.
        logger.error("[chat-turns] no se pudo guardar turno: %s", exc, exc_info=True)
        return None


def _parse_options(value: Any) -> List[Dict[str, Any]]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return [dict(item) for item in decoded if isinstance(item, dict)] if isinstance(decoded, list) else []


def _public_turn(record: Dict[str, Any]) -> Dict[str, Any]:
    result = {field: record.get(field) for field in CHAT_TURN_FIELDS}
    timestamp = result.get("timestamp")
    if isinstance(timestamp, datetime):
        result["timestamp"] = _as_utc_datetime(timestamp).isoformat()
    turn_date = result.get("fecha")
    if isinstance(turn_date, date):
        result["fecha"] = turn_date.isoformat()
    result["opciones"] = _parse_options(result.get("opciones"))
    return result


def _read_bigquery(
    session_id: Optional[str],
    start: Optional[date],
    end: Optional[date],
) -> List[Dict[str, Any]]:
    client = _get_client()
    clauses = []
    parameters = []
    if session_id:
        clauses.append("session_id = @session_id")
        parameters.append(bigquery.ScalarQueryParameter("session_id", "STRING", session_id))
    if start is not None and end is not None:
        clauses.append("fecha BETWEEN @start AND @end")
        parameters.extend(
            [
                bigquery.ScalarQueryParameter("start", "DATE", start),
                bigquery.ScalarQueryParameter("end", "DATE", end),
            ]
        )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT {', '.join(CHAT_TURN_FIELDS)}
        FROM `{_table_id(client)}`
        {where}
        ORDER BY session_id ASC, orden ASC
    """
    job_config = bigquery.QueryJobConfig(query_parameters=parameters)
    rows = client.query(query, job_config=job_config).result()
    return [
        _public_turn({field: _row_value(row, field) for field in CHAT_TURN_FIELDS})
        for row in rows
    ]


def _read_local(
    session_id: Optional[str],
    start: Optional[date],
    end: Optional[date],
) -> List[Dict[str, Any]]:
    result = []
    for record in _load_local_records():
        if session_id and str(record.get("session_id") or "") != session_id:
            continue
        record_date = _as_utc_datetime(record.get("timestamp")).date()
        if start is not None and end is not None and not (start <= record_date <= end):
            continue
        result.append(_public_turn(record))
    return sorted(result, key=lambda item: (str(item.get("session_id") or ""), int(item.get("orden") or 0)))


def read_turns(
    session_id: Optional[str] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Lee turnos para el endpoint; los errores se propagan como 503 desde ``bot.py``."""

    if _uses_bigquery():
        return _read_bigquery(session_id, start, end)
    with _TURN_LOCK:
        return _read_local(session_id, start, end)


def reset_runtime_state() -> None:
    """Reinicia cliente y caché; útil para aislar pruebas."""

    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None
    with _TURN_LOCK:
        _LAST_TURN_CACHE.clear()
