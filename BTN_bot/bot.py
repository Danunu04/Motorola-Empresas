"""
BTN_bot - Bot de WhatsApp con botones interactivos

Todos los flujos usan WhatsApp Interactive Buttons.
El usuario navega exclusivamente haciendo clic en botones,
excepto cuando debe ingresar un email (texto libre).

Endpoints:
- GET  /            - Status
- GET  /health      - Health check
- GET  /webhook     - WhatsApp webhook verification
- POST /webhook     - WhatsApp webhook receiver
- POST /chat        - Chat API
- POST /reset       - Reset session
- GET  /chatlog     - Get chat logs
- POST /agent/send  - Agent sends message
- POST /bot/send    - Bot sends manual message
- GET  /handoff/sessions - Get handoff sessions
- POST /resume      - Resume bot after handoff
- GET  /flow/states - Get registered flow states
- GET  /chatlog/download - Descarga de conversaciones por rango de fechas y session_id opcional (csv/json/pdf/txt)
- GET  /messages    - Listar mensajes editables del bot
- POST /messages    - Crear un nuevo mensaje editable
- PUT  /messages/{message_key} - Actualizar el contenido de un mensaje editable
- POST /messages/{message_key}/reset - Restaurar un mensaje editable a su valor por defecto
"""

import os
import httpx
import json
import re
import csv
import io
import time
import asyncio
import uuid
import logging
from datetime import date, datetime, timezone
from fastapi.responses import FileResponse
from typing import Any, Dict, List, Optional
from threading import Lock
from pathlib import Path
from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

import message_store
from flow_states_btn import (
    FlowController,
    FlowContext,
    FlowState,
    StateFactory,
    Button,
    ListRow,
    WELCOME_MESSAGE,
    EMAIL_RE,
)

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None

from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.responses import Response

load_dotenv()

# ==============================
# Logging Configuration
# ==============================

if os.getenv("CLOUD_RUN") == "true":
    import sys

    class GoogleCloudFormatter(logging.Formatter):
        def format(self, record):
            log_entry = {
                "severity": self._get_severity(record.levelname),
                "message": record.getMessage(),
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "logger": record.name,
            }
            if record.exc_info:
                log_entry["exception"] = self.formatException(record.exc_info)
            return json.dumps(log_entry)

        @staticmethod
        def _get_severity(levelname: str) -> str:
            return {
                "DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING",
                "ERROR": "ERROR", "CRITICAL": "CRITICAL",
            }.get(levelname, "DEFAULT")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(GoogleCloudFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

logger = logging.getLogger(__name__)

# ==============================
# Configuración
# ==============================

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
CHAT_MIN_SECONDS_BETWEEN_MESSAGES = float(os.getenv("CHAT_MIN_SECONDS_BETWEEN_MESSAGES", "3"))
DEFAULT_CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:4200",
    "http://127.0.0.1:4200",
    "http://localhost:8501",
    "http://127.0.0.1:8501",
    "https://front-log-995204915971.us-central1.run.app",
    "https://streamlit-service-995204915971.us-central1.run.app",
]


def _parse_cors_allowed_origins() -> List[str]:
    configured_origins = [
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    ]
    origins = configured_origins or DEFAULT_CORS_ALLOWED_ORIGINS
    return list(dict.fromkeys(origins))


CORS_ALLOWED_ORIGINS = _parse_cors_allowed_origins()

BASE_DIR = Path(__file__).resolve().parent

# ==============================
# Utilidades
# ==============================

EMAIL_RE = re.compile(r"[\w\.\-\+]+@[\w\.\-]+\.\w+", re.I)
NAME_STORE_PATH = BASE_DIR / "data" / "user_names.json"
NAME_STORE_LOCK = Lock()
CHAT_LOG_PATH = BASE_DIR / "logs" / "chat_log.json"
CHAT_LOG_LOCK = Lock()
BIGQUERY_CLIENT = None
BIGQUERY_CLIENT_LOCK = Lock()
BIGQUERY_CHAT_LOG_DATASET = os.getenv("BIGQUERY_CHAT_LOG_DATASET", "inspectia_logs")
BIGQUERY_CHAT_LOG_TABLE = os.getenv("BIGQUERY_CHAT_LOG_TABLE", "chat_history")
BIGQUERY_CHAT_LOG_ENABLED = os.getenv("BIGQUERY_CHAT_LOG_ENABLED", "").lower() in {"1", "true", "yes"}

NAME_STOPWORDS = {
    "hola", "buenas", "buenos", "dias", "día", "tardes", "noches",
    "gracias", "ok", "dale", "listo", "ayuda", "soporte",
}


def _normalize_name(raw: str) -> Optional[str]:
    if not raw:
        return None
    cleaned = re.sub(r"[^A-Za-zÁÉÍÓÚáéíóúÑñ' ]", " ", raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    words = cleaned.split(" ")[:3]
    if not words:
        return None
    lower_words = [w.lower() for w in words]
    if len(lower_words) == 1 and lower_words[0] in NAME_STOPWORDS:
        return None
    return " ".join(w.capitalize() for w in words)


def _extract_user_name(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    patterns = [
        r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚáéíóúÑñ' ]{2,40})",
        r"\bme llamo\s+([A-Za-zÁÉÍÓÚáéíóúÑñ' ]{2,40})",
        r"\bsoy\s+([A-Za-zÁÉÍÓÚáéíóúÑñ' ]{2,40})",
    ]
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            return _normalize_name(m.group(1))
    if re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúÑñ' ]{2,40}", t):
        return _normalize_name(t)
    return None


def _is_name_only_message(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False
    markers = ["no puedo", "registr", "ingresar", "precio", "mail", "@", "codigo", "soporte", "sesion"]
    return not any(m in t for m in markers)


def _strip_emojis(text: str) -> str:
    if not text:
        return text
    emoji_re = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "\U00002600-\U000027BF"
        "]+",
        flags=re.UNICODE,
    )
    out = emoji_re.sub("", text)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n[ \t]+", "\n", out)
    return out.strip()


def _capitalize_first(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    first = t[0]
    if first.isalpha() and first.islower():
        return first.upper() + t[1:]
    return t


def _load_name_store() -> Dict[str, str]:
    if not NAME_STORE_PATH.exists():
        return {}
    try:
        with open(NAME_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
        return {}


def _load_chat_log() -> List[Dict[str, Any]]:
    if not CHAT_LOG_PATH.exists():
        return []
    try:
        with open(CHAT_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                return []
            return [_normalize_chat_log_record(item) for item in data if isinstance(item, dict)]
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, OSError):
        return []


def _detect_chat_log_status(question: str, answer: str) -> str:
    clean_answer = str(answer or "").strip()
    if clean_answer == "[EN ESPERA DE AGENTE]":
        return "handoff_waiting"
    if question.startswith("[AGENTE]"):
        return "agent_sent"
    if question == "[BOT-MANUAL]":
        return "manual_bot_message"
    if not clean_answer:
        return "pending"
    return "answered"


def _normalize_chat_log_record(record: Dict[str, Any]) -> Dict[str, Any]:
    timestamp = record.get("timestamp")
    session_id = str(record.get("session_id") or "")
    question = str(record.get("question") or "")
    answer = str(record.get("answer") or "")
    channel = record.get("channel") or ("whatsapp" if session_id.startswith("wa:") else "api")
    environment = record.get("environment") or ("cloud_run" if os.getenv("CLOUD_RUN") == "true" else "local")
    turn_id = str(record.get("turn_id") or record.get("id") or uuid.uuid4())
    status = record.get("status") or _detect_chat_log_status(question, answer)

    return {
        "turn_id": turn_id,
        "timestamp": timestamp,
        "session_id": session_id,
        "channel": channel,
        "environment": environment,
        "status": status,
        "question": question,
        "answer": answer,
        "messages": [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ],
    }


def _should_use_bigquery_chat_log() -> bool:
    return BIGQUERY_CHAT_LOG_ENABLED and bool(os.getenv("GOOGLE_CLOUD_PROJECT"))


def _get_bigquery_table_id() -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    return f"{project}.{BIGQUERY_CHAT_LOG_DATASET}.{BIGQUERY_CHAT_LOG_TABLE}"


def _ensure_bigquery_chat_table(client) -> None:
    dataset_id = f"{client.project}.{BIGQUERY_CHAT_LOG_DATASET}"
    table_id = _get_bigquery_table_id()

    dataset = bigquery.Dataset(dataset_id)
    dataset.location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    client.create_dataset(dataset, exists_ok=True)

    schema = [
        bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("question", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("answer", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("channel", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("environment", "STRING", mode="NULLABLE"),
    ]
    table = bigquery.Table(table_id, schema)
    client.create_table(table, exists_ok=True)


def _get_bigquery_client():
    global BIGQUERY_CLIENT

    if bigquery is None:
        raise RuntimeError("google-cloud-bigquery no está instalado")

    with BIGQUERY_CLIENT_LOCK:
        if BIGQUERY_CLIENT is None:
            BIGQUERY_CLIENT = bigquery.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))
            _ensure_bigquery_chat_table(BIGQUERY_CLIENT)
        return BIGQUERY_CLIENT


def _append_chat_log_bigquery(record: Dict[str, Any]) -> None:
    client = _get_bigquery_client()
    bq_record = {
        "timestamp": record["timestamp"],
        "session_id": record["session_id"],
        "question": record["question"],
        "answer": record["answer"],
        "channel": record["channel"],
        "environment": record["environment"],
    }
    errors = client.insert_rows_json(_get_bigquery_table_id(), [bq_record])
    if errors:
        raise RuntimeError(str(errors))


def _read_chat_log_bigquery(limit: int = 500) -> List[Dict[str, Any]]:
    client = _get_bigquery_client()
    query = f"""
        SELECT timestamp, session_id, question, answer, channel, environment
        FROM `{_get_bigquery_table_id()}`
        ORDER BY timestamp DESC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    rows = client.query(query, job_config=job_config).result()
    logs = []
    for row in rows:
        logs.append(
            _normalize_chat_log_record(
                {
                    "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                    "session_id": row["session_id"],
                    "question": row["question"],
                    "answer": row["answer"],
                    "channel": row["channel"],
                    "environment": row["environment"],
                }
            )
        )
    return logs


CHATLOG_COLUMNS = [
    "turn_id", "timestamp", "session_id", "channel", "environment", "status", "question", "answer",
]


def _parse_date_param(value: str, name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"'{name}' debe tener formato YYYY-MM-DD")


def _filter_chat_log_by_range(
    logs: List[Dict[str, Any]], start: date, end: date, session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    result = []
    for record in logs:
        timestamp = record.get("timestamp")
        if not timestamp:
            continue
        try:
            record_date = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if not (start <= record_date <= end):
            continue
        if session_id and str(record.get("session_id", "")) != session_id:
            continue
        result.append(record)
    return result


def _read_chat_log_bigquery_range(
    start: date, end: date, session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    client = _get_bigquery_client()
    where_clauses = ["DATE(timestamp) BETWEEN @start AND @end"]
    query_parameters = [
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end),
    ]
    if session_id:
        where_clauses.append("session_id = @session_id")
        query_parameters.append(bigquery.ScalarQueryParameter("session_id", "STRING", session_id))

    query = f"""
        SELECT timestamp, session_id, question, answer, channel, environment
        FROM `{_get_bigquery_table_id()}`
        WHERE {' AND '.join(where_clauses)}
        ORDER BY timestamp ASC
    """
    job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
    rows = client.query(query, job_config=job_config).result()
    logs = []
    for row in rows:
        logs.append(
            _normalize_chat_log_record(
                {
                    "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                    "session_id": row["session_id"],
                    "question": row["question"],
                    "answer": row["answer"],
                    "channel": row["channel"],
                    "environment": row["environment"],
                }
            )
        )
    return logs


def _build_chatlog_pdf(logs: List[Dict[str, Any]], start: date, end: date) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4))
    styles = getSampleStyleSheet()

    header = Paragraph(f"Conversaciones {start.isoformat()} a {end.isoformat()}", styles["Title"])
    data = [CHATLOG_COLUMNS] + [
        [str(record.get(col, ""))[:200] for col in CHATLOG_COLUMNS] for record in logs
    ]
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003a70")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
    ]))

    doc.build([header, Spacer(1, 12), table])
    return buf.getvalue()


def _build_chatlog_txt(logs: List[Dict[str, Any]]) -> str:
    from collections import OrderedDict

    grouped: OrderedDict[str, List[Dict[str, Any]]] = OrderedDict()
    for record in logs:
        session_id = str(record.get("session_id") or "")
        grouped.setdefault(session_id, []).append(record)

    lines = []
    for index, (session_id, records) in enumerate(grouped.items(), start=1):
        lines.append(f"Chat {index}: {session_id}")
        lines.append("-" * max(20, len(session_id) + 8))
        for record in records:
            question = str(record.get("question") or "")
            answer = str(record.get("answer") or "")
            lines.append(f"Pregunta: {question}")
            lines.append(f"Respuesta: {answer}")
            lines.append("")

    return "\n".join(lines)


def _append_chat_log(session_id: str, question: str, answer: str) -> None:
    record = _normalize_chat_log_record({
        "turn_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "question": question,
        "answer": answer,
        "channel": "whatsapp" if session_id.startswith("wa:") else "api",
        "environment": "cloud_run" if os.getenv("CLOUD_RUN") == "true" else "local",
    })
    try:
        if _should_use_bigquery_chat_log():
            _append_chat_log_bigquery(record)
            return

        with CHAT_LOG_LOCK:
            logs = _load_chat_log()
            logs.append(record)
            CHAT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CHAT_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
    except (OSError, json.JSONEncodeError) as e:
        logger.error(f"[chat-log] error: {e}")


def _get_saved_name(session_id: str) -> Optional[str]:
    with NAME_STORE_LOCK:
        saved = _load_name_store().get(session_id)
    normalized = _normalize_name(saved or "")
    return normalized


def _save_name(session_id: str, name: str) -> None:
    with NAME_STORE_LOCK:
        store = _load_name_store()
        store[session_id] = name
        NAME_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(NAME_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)


# ==============================
# Dominios de Empresa
# ==============================

def _default_domains_csv_path() -> str:
    envp = os.getenv("DOMAINS_CSV_PATH")
    if envp:
        return envp

    p1 = "/mnt/data/BOT DOMINIOS.csv"
    if os.path.exists(p1):
        return p1

    p2 = str(BASE_DIR / "BOT DOMINIOS.csv")
    if os.path.exists(p2):
        return p2

    return str(BASE_DIR / "data" / "DOMINIOS.csv")


COMPANY_DOMAIN_TO_CODE: dict[str, str] = {}
COMPANY_DOMAINS: set[str] = set()


def load_company_domains_from_csv(csv_path: str) -> tuple[dict[str, str], set[str]]:
    mapping: dict[str, str] = {}
    domains: set[str] = set()

    if not csv_path or not os.path.exists(csv_path):
        logger.warning(f"[domains] CSV no encontrado: {csv_path}")
        return mapping, domains

    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            delim = ";" if sample.count(";") >= sample.count(",") else ","
            reader = csv.reader(f, delimiter=delim)

            for row in reader:
                if not row or len(row) < 2:
                    continue

                code = (row[0] or "").strip()
                domain = (row[1] or "").strip().lower()

                if code.lower() in {"codigo", "code", "cod"} and domain.lower() in {"dominio", "domain"}:
                    continue

                domain = domain.lstrip("@")

                if not domain:
                    continue

                mapping[domain] = code
                domains.add(domain)

        logger.info(f"[domains] cargados {len(domains)} dominios desde {csv_path}")
        return mapping, domains

    except (csv.Error, FileNotFoundError, PermissionError, OSError) as e:
        logger.error(f"[domains] error leyendo CSV ({csv_path}): {e}")
        return {}, set()


def load_company_domains(csv_path: Optional[str] = None) -> tuple[dict[str, str], set[str]]:
    if csv_path is None:
        csv_path = _default_domains_csv_path()
    return load_company_domains_from_csv(csv_path)


def is_company_domain(domain: str) -> bool:
    d = (domain or "").lower().strip()
    if not d:
        return False
    return (d in COMPANY_DOMAINS) or any(d.endswith("." + x) for x in COMPANY_DOMAINS)


DOMAINS_CSV_PATH = _default_domains_csv_path()
COMPANY_DOMAIN_TO_CODE, COMPANY_DOMAINS = load_company_domains(DOMAINS_CSV_PATH)


# ==============================
# Utilidades de Estado
# ==============================

FLOW_SPEC = {}  # Los flujos ahora están en flow_states_btn.py

def _reply_triggers_handoff(reply: str) -> bool:
    if not reply:
        return False
    text = reply.lower()
    handoff_keywords = [
        "te voy a derivar", "te derivo", "derivado con", "derivada con",
        "persona más calificada", "persona más capacitada",
        "asesor humano", "agente humano", "operador"
    ]
    return any(keyword in text for keyword in handoff_keywords)


# ==============================
# Models
# ==============================

class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="ID de sesión (ej: uuid).")
    message: str = Field(..., min_length=1)
    debug: bool = False


class DocDebug(BaseModel):
    i: int
    source: str
    preview: str


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    took_ms: int
    buttons: Optional[List[Dict[str, Any]]] = None
    interactive_type: Optional[str] = None
    list_config: Optional[Dict[str, Any]] = None
    debug_docs: Optional[List[DocDebug]] = None


class ResetRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class MessageUpdateRequest(BaseModel):
    content: str
    updated_by: str = ""


class MessageResetBody(BaseModel):
    updated_by: str = ""


class MessageCreateRequest(BaseModel):
    message_key: str
    message_type: str
    state_name: Optional[str] = None
    flujo_identificacion_mensaje: Optional[str] = None
    label: Optional[str] = None
    content: str
    default_content: Optional[str] = None
    updated_by: str = ""
    orden: Optional[int] = None


class MessageReorderRequest(BaseModel):
    orders: List[Dict[str, Any]]


# ==============================
# Session Management
# ==============================

def _new_session_state() -> Dict[str, Any]:
    return {
        "history": [],
        "ui_messages": [],
        "started": True,
        "user_name": None,
        "asked_name": False,
        "already_greeted": False,
        "flow_context": None,
        "human_handoff": False,
        "last_user_message_at": 0.0,
    }


def normalize_phone(number: str) -> str:
    if not number:
        return ""
    number = re.sub(r"\D", "", number)
    if number.startswith("549"):
        return "54" + number[3:]
    return number


# ==============================
# FastAPI App
# ==============================

app = FastAPI(title="Motorola BTN Bot API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Estado en memoria por sesión
SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSIONS_LOCK = Lock()

# Controlador de flujo global
FLOW_CONTROLLER: Optional[FlowController] = None


def get_session(session_id: str) -> Dict[str, Any]:
    with SESSIONS_LOCK:
        if session_id not in SESSIONS:
            SESSIONS[session_id] = _new_session_state()
        return SESSIONS[session_id]


def _is_rate_limited(state: Dict[str, Any]) -> bool:
    cooldown = max(CHAT_MIN_SECONDS_BETWEEN_MESSAGES, 0)
    if cooldown <= 0:
        return False

    last_message_at = float(state.get("last_user_message_at") or 0.0)
    now = time.monotonic()
    if last_message_at and (now - last_message_at) < cooldown:
        return True

    state["last_user_message_at"] = now
    return False


# ==============================
# WhatsApp Messaging
# ==============================

async def send_whatsapp_text(to: str, text: str) -> None:
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("[whatsapp] faltan credenciales para enviar mensaje")
        return

    normalized_to = normalize_phone(to)

    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "text",
        "text": {"body": (text or "")[:4096]},
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp send failed: {r.status_code} {r.text}")


async def send_whatsapp_interactive(to: str, text: str, buttons: List[Dict[str, Any]]) -> None:
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("[whatsapp] faltan credenciales para enviar mensaje interactivo")
        return

    if not buttons:
        await send_whatsapp_text(to, text)
        return

    normalized_to = normalize_phone(to)

    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (text or "")[:1024]
            },
            "action": {
                "buttons": buttons[:3]  # WhatsApp limita a 3 botones
            }
        }
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp interactive send failed: {r.status_code} {r.text}")


async def send_whatsapp_list(to: str, text: str, list_config: Dict[str, Any]) -> None:
    """Envía un List Message de WhatsApp (type: list).

    list_config formato:
    {
        "button_text": "Ver opciones",   # ≤ 20 chars
        "sections": [
            {
                "title": "Sección",       # ≤ 24 chars
                "rows": [
                    {"id": "row_id", "title": "Título", "description": "Desc"},
                    ...
                ]
            }
        ]
    }
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("[whatsapp] faltan credenciales para enviar list message")
        return

    normalized_to = normalize_phone(to)

    button_text = (list_config.get("button_text") or "Opciones")[:20]
    sections_raw = list_config.get("sections", [])

    sections = []
    for section in sections_raw[:1]:  # WhatsApp soporta múltiples secciones, usamos 1 para menú simple
        sec = {"title": (section.get("title") or "")[:24]}
        rows = []
        for row in section.get("rows", [])[:10]:  # Máximo 10 filas por sección
            row_dict = {"id": row.get("id", ""), "title": str(row.get("title", ""))[:24]}
            if row.get("description"):
                row_dict["description"] = str(row["description"])[:72]
            rows.append(row_dict)
        sec["rows"] = rows
        sections.append(sec)

    if not sections or not sections[0].get("rows"):
        await send_whatsapp_text(to, text)
        return

    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": (text or "")[:1024]
            },
            "action": {
                "button": button_text,
                "sections": sections,
            }
        }
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp list send failed: {r.status_code} {r.text}")


# ==============================
# Endpoints
# ==============================

@app.get("/")
def root():
    return {"message": "Motorola BTN Bot API is running. Use /chat to interact."}


@app.get("/health")
def health():
    flow_ok = FLOW_CONTROLLER is not None
    return {
        "ok": flow_ok,
        "flow_ok": flow_ok,
        "project": os.getenv("GOOGLE_CLOUD_PROJECT"),
        "location": os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
        "flow_states_count": len(StateFactory.get_registered_states()),
    }


@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "", status_code=200)

    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/reset")
def reset(req: ResetRequest):
    with SESSIONS_LOCK:
        SESSIONS[req.session_id] = _new_session_state()
    return {"ok": True, "session_id": req.session_id}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    t0 = time.time()

    q = (req.message or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="message vacío")

    state = get_session(req.session_id)

    if _is_rate_limited(state):
        return ChatResponse(
            session_id=req.session_id,
            answer="Esperá un momento antes de mandar otro mensaje, así puedo procesarlo bien.",
            took_ms=int((time.time() - t0) * 1000),
        )

    if not state.get("user_name"):
        saved_name = _get_saved_name(req.session_id)
        if saved_name:
            state["user_name"] = saved_name

    def _respond(
        answer: str,
        buttons: Optional[List[Dict[str, Any]]] = None,
        interactive_type: Optional[str] = None,
        list_config: Optional[Dict[str, Any]] = None,
    ) -> ChatResponse:
        final_answer = str(answer or "").strip()
        final_answer = _strip_emojis(final_answer)
        final_answer = _capitalize_first(final_answer)

        _append_chat_log(req.session_id, q, final_answer)

        return ChatResponse(
            session_id=req.session_id,
            answer=final_answer,
            took_ms=int((time.time() - t0) * 1000),
            buttons=buttons,
            interactive_type=interactive_type,
            list_config=list_config,
        )

    if state.get("human_handoff"):
        return _respond(
            "Ya te derivé con una persona más calificada. En breve te va a continuar un asesor humano."
        )

    if q.lower() == "exit":
        return _respond("Hasta la próxima 😊")

    # ==============================
    # FLOW con botones
    # ==============================
    try:
        global FLOW_CONTROLLER, COMPANY_DOMAIN_TO_CODE
        if FLOW_CONTROLLER is None:
            COMPANY_DOMAIN_TO_CODE, COMPANY_DOMAINS_DATA = load_company_domains()
            FLOW_CONTROLLER = FlowController(FLOW_SPEC, COMPANY_DOMAIN_TO_CODE)
            logger.info(f"[chat] FlowController inicializado con {len(StateFactory.get_registered_states())} estados registrados")

        flow_context = state.get("flow_context")
        if flow_context is None:
            flow_context = FLOW_CONTROLLER.create_context()
            state["flow_context"] = flow_context

        decision = FLOW_CONTROLLER.process_message(flow_context, q)

        mode = (decision.get("mode") or "flow").strip().lower()
        reply = (decision.get("reply") or "").strip()
        buttons = decision.get("buttons")
        interactive_type = decision.get("interactive_type", "button")
        list_config = decision.get("list_config")

        if mode == "flow" and reply:
            current_flow_state = flow_context.get_state()
            has_handoff = bool(decision.get("handoff")) or _reply_triggers_handoff(reply)

            if current_flow_state is None:
                state["flow_context"] = None
            else:
                state["flow_context"] = flow_context

            if has_handoff:
                state["human_handoff"] = True

            return _respond(reply, buttons=buttons, interactive_type=interactive_type, list_config=list_config)

    except (ValueError, KeyError, RuntimeError) as e:
        logger.error(f"[flow error] {e}")

    return _respond("Ante esa problemática comunicate por WhatsApp. 😊")


@app.post("/webhook")
async def webhook_receive(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        logger.error(f"[webhook json error] {e}")
        return {"ok": True, "note": "invalid json"}

    try:
        entry = body["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        messages = value.get("messages", [])
        if not messages:
            return {"ok": True}

        msg = messages[0]
        from_number = normalize_phone(msg.get("from", ""))
        msg_type = msg.get("type")

        # Manejar respuesta de botón o lista
        if msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                button_reply = interactive.get("button_reply", {})
                button_id = button_reply.get("id", "")
                button_title = button_reply.get("title", "")
                text = f"button_{button_id}"
                logger.info(f"[webhook] button clicked: {button_id} ({button_title})")
            elif interactive.get("type") == "list_reply":
                list_reply = interactive.get("list_reply", {})
                list_id = list_reply.get("id", "")
                list_title = list_reply.get("title", "")
                text = f"button_{list_id}"
                logger.info(f"[webhook] list item selected: {list_id} ({list_title})")
            else:
                await send_whatsapp_text(from_number, "Por ahora solo puedo leer mensajes de texto y botones 😊💙")
                return {"ok": True}
        elif msg_type == "text":
            text = (msg.get("text", {}).get("body") or "").strip()
        else:
            await send_whatsapp_text(from_number, "Por ahora solo puedo leer mensajes de texto 😊💙")
            return {"ok": True}

        if not from_number or not text:
            return {"ok": True}

    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"[webhook parse error] {e}")
        return {"ok": True, "note": "payload not recognized"}

    session_id = f"wa:{from_number}"

    if get_session(session_id).get("human_handoff"):
        _append_chat_log(session_id, text, "[EN ESPERA DE AGENTE]")
        return {"ok": True, "to": from_number, "in": text, "handoff": True}

    try:
        req_obj = ChatRequest(
            session_id=session_id,
            message=text,
            debug=False,
        )
        resp: ChatResponse = await run_in_threadpool(chat, req_obj)
        answer = resp.answer

        interactive_type = resp.interactive_type or "button"
        buttons = resp.buttons
        list_config = resp.list_config

        if interactive_type == "list" and list_config:
            try:
                await send_whatsapp_list(from_number, answer, list_config)
            except (RuntimeError, Exception) as list_err:
                logger.warning(f"[webhook] list message failed, falling back to buttons: {list_err}")
                # Fallback: enviar las primeras 3 opciones como Reply Buttons
                fallback_buttons = []
                for section in list_config.get("sections", []):
                    for row in section.get("rows", [])[:3]:
                        fallback_buttons.append({
                            "type": "reply",
                            "reply": {"id": row.get("id", ""), "title": str(row.get("title", ""))[:20]}
                        })
                if fallback_buttons:
                    await send_whatsapp_interactive(from_number, answer, fallback_buttons)
                else:
                    await send_whatsapp_text(from_number, answer)
        elif buttons and isinstance(buttons, list) and all(isinstance(b, dict) for b in buttons):
            await send_whatsapp_interactive(from_number, answer, buttons)
        else:
            await send_whatsapp_text(from_number, answer)

    except (HTTPException, RuntimeError, Exception) as e:
        logger.error(f"[webhook chat error] {e}")
        answer = "Perdón, no pude procesar tu mensaje en este momento."
        try:
            await send_whatsapp_text(from_number, answer)
        except Exception:
            pass

    return {"ok": True, "to": from_number, "in": text, "answer": answer}


@app.post("/resume")
def resume_bot(req: ResetRequest):
    logger.info(f"[resume] session_id: {req.session_id}")
    state = get_session(req.session_id)
    state["human_handoff"] = False
    state["flow_context"] = None
    return {"ok": True, "session_id": req.session_id, "human_handoff": False}


@app.get("/chatlog")
def get_chat_log():
    if _should_use_bigquery_chat_log():
        try:
            logs = _read_chat_log_bigquery()
            return JSONResponse(content={"ok": True, "source": "bigquery", "logs": logs})
        except (bigquery.exceptions.BigQueryError, RuntimeError) as e:
            raise HTTPException(status_code=503, detail=f"No se pudo leer chatlog desde BigQuery: {e}")

    if not CHAT_LOG_PATH.exists():
        return {"ok": False, "message": "No hay logs aún"}

    return FileResponse(
        CHAT_LOG_PATH,
        media_type="application/json",
        filename="chat_log.json"
    )


@app.get("/chatlog/download")
def download_chat_log(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    format: str = Query("csv"),
    session_id: Optional[str] = Query(None),
):
    if not from_ or not to:
        raise HTTPException(status_code=422, detail="Los parámetros 'from' y 'to' son obligatorios")

    start = _parse_date_param(from_, "from")
    end = _parse_date_param(to, "to")

    if start > end:
        raise HTTPException(status_code=400, detail="'from' no puede ser posterior a 'to'")

    fmt = format.lower()
    if fmt not in {"csv", "json", "pdf", "txt"}:
        raise HTTPException(status_code=422, detail="'format' debe ser csv, json, pdf o txt")

    if fmt == "txt" and not _should_use_bigquery_chat_log():
        raise HTTPException(
            status_code=503,
            detail="BigQuery no está configurado; la exportación .txt requiere la tabla chat_history",
        )

    if _should_use_bigquery_chat_log():
        try:
            logs = _read_chat_log_bigquery_range(start, end, session_id=session_id)
        except (RuntimeError, Exception) as e:
            raise HTTPException(status_code=503, detail=f"No se pudo leer chatlog desde BigQuery: {e}")
    else:
        logs = _filter_chat_log_by_range(_load_chat_log(), start, end, session_id=session_id)

    if not logs:
        raise HTTPException(status_code=404, detail="No hay conversaciones en el rango solicitado")

    filename_base = f"chatlog_{start.isoformat()}_{end.isoformat()}"

    if fmt == "json":
        payload = [{col: record.get(col) for col in CHATLOG_COLUMNS} for record in logs]
        return JSONResponse(
            content=payload,
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'},
        )

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CHATLOG_COLUMNS)
        writer.writeheader()
        for record in logs:
            writer.writerow({col: record.get(col, "") for col in CHATLOG_COLUMNS})
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'},
        )

    if fmt == "txt":
        txt_content = _build_chatlog_txt(logs)
        return Response(
            content=txt_content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.txt"'},
        )

    pdf_bytes = _build_chatlog_pdf(logs, start, end)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
    )


@app.get("/messages")
def list_messages():
    return {"ok": True, "messages": message_store.get_all_messages_with_metadata()}


@app.put("/messages/{message_key}")
def update_message(message_key: str, req: MessageUpdateRequest):
    meta = message_store.get_message_metadata(message_key)
    if meta is None:
        raise HTTPException(status_code=404, detail="message_key no registrada")

    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content vacío")

    limit = message_store.MESSAGE_LIMITS.get(meta["message_type"])
    if limit and len(content) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"content excede el límite de {limit} caracteres para {meta['message_type']}",
        )

    try:
        message_store.set_message(message_key, content, req.updated_by)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"BigQuery no disponible para guardar cambios: {e}")

    logger.info(f"[messages] {message_key} actualizado por {req.updated_by}")
    return {"ok": True, "message_key": message_key}


@app.post("/messages/{message_key}/reset")
def reset_message(message_key: str, req: MessageResetBody):
    meta = message_store.get_message_metadata(message_key)
    if meta is None:
        raise HTTPException(status_code=404, detail="message_key no registrada")

    try:
        message_store.set_message(message_key, meta["default_content"], req.updated_by)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"BigQuery no disponible para guardar cambios: {e}")

    logger.info(f"[messages] {message_key} restaurado a default por {req.updated_by}")
    return {"ok": True, "message_key": message_key}


@app.post("/messages")
def create_message(req: MessageCreateRequest):
    if message_store.get_message_metadata(req.message_key) is not None:
        raise HTTPException(status_code=409, detail="message_key ya existe")

    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content vacío")

    if req.message_type not in message_store.MESSAGE_LIMITS:
        raise HTTPException(status_code=422, detail=f"message_type '{req.message_type}' no es válido")

    limit = message_store.MESSAGE_LIMITS.get(req.message_type)
    if limit and len(content) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"content excede el límite de {limit} caracteres para {req.message_type}",
        )

    try:
        message_store.create_message(
            message_key=req.message_key,
            message_type=req.message_type,
            state_name=req.state_name,
            flujo_identificacion_mensaje=req.flujo_identificacion_mensaje,
            label=req.label,
            content=content,
            default_content=req.default_content,
            updated_by=req.updated_by,
            orden=req.orden,
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=503, detail=f"No se pudo crear el mensaje: {e}")

    logger.info(f"[messages] {req.message_key} creado por {req.updated_by}")
    return {"ok": True, "message_key": req.message_key}


@app.patch("/messages/reorder")
def reorder_messages(req: MessageReorderRequest):
    if not req.orders:
        return {"ok": True, "updated": 0}

    try:
        message_store.update_message_order(req.orders)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"BigQuery no disponible para reordenar: {e}")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"message_key no registrada: {e}")

    logger.info(f"[messages] reordenados {len(req.orders)} mensajes")
    return {"ok": True, "updated": len(req.orders)}


class AgentSendRequest(BaseModel):
    session_id: str
    message: Optional[str] = None
    handoff: Optional[bool] = False


@app.post("/agent/send")
async def agent_send(req: AgentSendRequest):
    phone = req.session_id.replace("wa:", "")
    await send_whatsapp_text(phone, req.message)
    _append_chat_log(req.session_id, f"[AGENTE] {req.message}", "")
    return {"ok": True}


@app.get("/handoff/sessions")
def get_handoff_sessions():
    with SESSIONS_LOCK:
        result = []
        for session_id, state in SESSIONS.items():
            if state.get("human_handoff"):
                result.append({
                    "session_id": session_id,
                    "phone": session_id.replace("wa:", ""),
                    "user_name": state.get("user_name"),
                })
    return {"ok": True, "sessions": result}


@app.post("/bot/send")
async def bot_send(req: AgentSendRequest):
    """Envia un mensaje pre-escrito como si fuera el bot (sin pasar por LLM)"""
    phone = req.session_id.replace("wa:", "")

    if req.handoff:
        state = get_session(req.session_id)
        state["human_handoff"] = True
        _append_chat_log(req.session_id, "[EN ESPERA DE AGENTE]", "")
        return {"ok": True}

    if req.message:
        await send_whatsapp_text(phone, req.message)
        _append_chat_log(req.session_id, "[BOT-MANUAL]", req.message)
        return {"ok": True}

    return {"ok": True}


@app.get("/flow/states")
def get_flow_states():
    states = StateFactory.get_registered_states()
    result = []
    for state_name in states:
        state_class = StateFactory._states.get(state_name)
        result.append({
            "state_name": state_name,
            "state_class": state_class.__name__ if state_class else None,
        })
    return {"ok": True, "states": result, "count": len(result)}


# ==============================
# Startup
# ==============================

@app.on_event("startup")
async def startup():
    global COMPANY_DOMAIN_TO_CODE, COMPANY_DOMAINS, FLOW_CONTROLLER

    COMPANY_DOMAIN_TO_CODE, COMPANY_DOMAINS = load_company_domains()

    FLOW_CONTROLLER = FlowController(FLOW_SPEC, COMPANY_DOMAIN_TO_CODE)
    logger.info(f"[startup] FlowController inicializado con {len(StateFactory.get_registered_states())} estados")

    message_store.load_all_messages()


# ==============================
# Main
# ==============================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)