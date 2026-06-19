# 📱 BTN Bot — Motorola Plataforma de Beneficios

Bot de WhatsApp con botones interactivos para soporte de la Plataforma de Beneficios de Motorola. El usuario navega exclusivamente a través de botones y listas interactivas, excepto cuando debe ingresar un email.

---

## Índice

- [Arquitectura](#arquitectura)
- [Estructura de archivos](#estructura-de-archivos)
- [Requisitos](#requisitos)
- [Variables de entorno](#variables-de-entorno)
- [Instalación y ejecución local](#instalación-y-ejecución-local)
- [API — Endpoints](#api--endpoints)
- [Flujos del bot](#flujos-del-bot)
- [BigQuery](#bigquery)
- [Frontend de prueba (Streamlit)](#frontend-de-prueba-streamlit)
- [Deploy en Google Cloud Run](#deploy-en-google-cloud-run)
- [Roadmap](#roadmap)

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│                        WhatsApp Cloud API                    │
│              (Meta Graph API v20.0 — webhooks)               │
└────────────────────────────┬────────────────────────────────┘
                             │ POST /webhook
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI — bot.py                          │
│                                                              │
│  /chat ──► FlowController ──► FlowState (handle)            │
│                │                    │                        │
│                │              flow_states_btn.py             │
│                │                    │                        │
│                ▼                    ▼                        │
│         SessionState          BigQuery                       │
│         (en memoria)     (chat_history / bot_messages)       │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│              Streamlit — streamlit_app.py                    │
│         (interfaz de prueba + admin + descarga logs)         │
└─────────────────────────────────────────────────────────────┘
```

El bot recibe mensajes a través del webhook de WhatsApp Cloud API. Cada mensaje pasa por el `FlowController`, que determina el estado actual de la sesión del usuario y delega el procesamiento al `FlowState` correspondiente. Las respuestas se envían de vuelta a WhatsApp como mensajes interactivos (botones o listas).

---

## Estructura de archivos

```
.
├── bot.py                  # API FastAPI — endpoints, webhook, lógica principal
├── flow_states_btn.py      # Máquina de estados del bot (flujos, botones, listas)
├── message_store.py        # (próximo) Cache + acceso a BigQuery para mensajes editables
├── streamlit_app.py        # Frontend de prueba y administración
├── requirements.txt        # Dependencias Python
├── .env                    # Variables de entorno (no commitear)
├── data/
│   ├── DOMINIOS.csv        # Dominios corporativos válidos (código, dominio)
│   └── user_names.json     # Cache de nombres de usuario (generado en runtime)
└── logs/
    └── chat_log.json       # Log local de conversaciones (cuando BigQuery está desactivado)
```

---

## Requisitos

- Python 3.11+
- Cuenta de WhatsApp Business con acceso a la Cloud API (Meta)
- Proyecto en Google Cloud Platform con BigQuery habilitado (opcional pero recomendado)
- Credenciales de GCP configuradas (`GOOGLE_APPLICATION_CREDENTIALS` o rol de servicio en Cloud Run)

---

## Variables de entorno

Crear un archivo `.env` en la raíz del proyecto con las siguientes variables:

```env
# ── WhatsApp Cloud API ──────────────────────────────────────
WHATSAPP_VERIFY_TOKEN=tu_token_de_verificacion_del_webhook
WHATSAPP_TOKEN=tu_bearer_token_de_la_api_de_whatsapp
WHATSAPP_PHONE_NUMBER_ID=tu_phone_number_id

# ── Google Cloud / BigQuery ─────────────────────────────────
GOOGLE_CLOUD_PROJECT=tu-proyecto-gcp
GOOGLE_CLOUD_LOCATION=us-central1

# Habilitar escritura y lectura del chat log en BigQuery
BIGQUERY_CHAT_LOG_ENABLED=true

# Nombre del dataset y tabla en BigQuery
BIGQUERY_CHAT_LOG_DATASET=inspectia_logs
BIGQUERY_CHAT_LOG_TABLE=chat_history

# ── Comportamiento del bot ───────────────────────────────────
# Tiempo mínimo en segundos entre mensajes del mismo usuario (rate limiting)
CHAT_MIN_SECONDS_BETWEEN_MESSAGES=3

# ── CORS ─────────────────────────────────────────────────────
# Orígenes permitidos separados por coma (opcional, tiene defaults)
CORS_ALLOWED_ORIGINS=http://localhost:8501,https://tu-frontend.run.app

# ── Cloud Run ────────────────────────────────────────────────
# Activa logging estructurado para Google Cloud Logging
CLOUD_RUN=true

# ── Dominios corporativos ────────────────────────────────────
# Ruta al CSV de dominios (opcional, tiene paths por defecto)
DOMAINS_CSV_PATH=/ruta/a/BOT DOMINIOS.csv
```

### Variables requeridas para producción

| Variable | Requerida | Descripción |
|----------|-----------|-------------|
| `WHATSAPP_VERIFY_TOKEN` | Sí | Token para verificar el webhook con Meta |
| `WHATSAPP_TOKEN` | Sí | Bearer token de la API de WhatsApp |
| `WHATSAPP_PHONE_NUMBER_ID` | Sí | ID del número de teléfono de WhatsApp Business |
| `GOOGLE_CLOUD_PROJECT` | Sí (con BQ) | ID del proyecto de GCP |
| `BIGQUERY_CHAT_LOG_ENABLED` | No | Activar logs en BigQuery (`true`/`false`) |

---

## Instalación y ejecución local

### 1. Clonar el repositorio y crear el entorno virtual

```bash
git clone <url-del-repo>
cd btn-bot
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows
```

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar variables de entorno

```bash
cp .env.example .env
# Editar .env con tus credenciales
```

### 4. Levantar la API

```bash
uvicorn bot:app --reload --host 0.0.0.0 --port 8000
```

La API estará disponible en `http://localhost:8000`.

### 5. Levantar el frontend de prueba (opcional)

```bash
streamlit run streamlit_app.py
```

El frontend estará disponible en `http://localhost:8501`.

### 6. Exponer el webhook localmente (para probar con WhatsApp)

Para recibir mensajes reales de WhatsApp durante el desarrollo, usar `ngrok`:

```bash
ngrok http 8000
```

Configurar la URL `https://<tu-id>.ngrok.io/webhook` como webhook en el panel de Meta for Developers.

---

## API — Endpoints

### Conversación

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/` | Status de la API |
| `GET` | `/health` | Health check con estado del FlowController |
| `POST` | `/chat` | Enviar mensaje al bot (para testing) |
| `POST` | `/reset` | Resetear sesión de usuario |

#### `POST /chat`

```json
// Request
{
  "session_id": "uuid-de-la-sesion",
  "message": "hola",
  "debug": false
}

// Response
{
  "session_id": "uuid-de-la-sesion",
  "answer": "Hola, gracias por comunicarte con la Plataforma de Beneficios de Motorola...",
  "took_ms": 12,
  "buttons": null,
  "interactive_type": "list",
  "list_config": { ... }
}
```

### WhatsApp Webhook

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/webhook` | Verificación del webhook por Meta |
| `POST` | `/webhook` | Recepción de mensajes de WhatsApp |

### Logs y Administración

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/chatlog` | Obtener log de conversaciones (BigQuery o archivo local) |
| `GET` | `/chatlog/download` | Descargar conversaciones por rango de fechas *(próximo)* |
| `GET` | `/flow/states` | Listar estados registrados en el FlowController |
| `GET` | `/handoff/sessions` | Listar sesiones en espera de agente humano |
| `POST` | `/agent/send` | Enviar mensaje como agente humano |
| `POST` | `/bot/send` | Enviar mensaje manual como bot (sin pasar por el flujo) |
| `POST` | `/resume` | Reanudar el bot después de un handoff a agente |

### Mensajes editables *(próximo)*

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/messages` | Listar todos los textos y botones editables |
| `PUT` | `/messages/{key}` | Actualizar el contenido de un mensaje |
| `POST` | `/messages/{key}/reset` | Restaurar un mensaje a su valor por defecto |

---

## Flujos del bot

El bot implementa una máquina de estados definida en `flow_states_btn.py`. Cada estado hereda de `FlowState` y se registra con el decorador `@StateFactory.register("NombreDelEstado")`.

### Estados registrados

| Estado | Descripción |
|--------|-------------|
| `EstadoInicial` | Menú principal con List Message (4 opciones) |
| `EstadoPreFlujo` | Mensaje de empatía intermedio antes de cada flujo |
| `EstadoPedirMail` | Solicita el email corporativo del usuario |
| `EstadoLogin` | Guía de inicio de sesión paso a paso |
| `EstadoFormulario` | Guía del formulario de registro |
| `EstadoNoVeoDescuentos` | Flujo para problema de descuentos no visibles |
| `EstadoInfoPedido` | Flujo de consulta de estado de pedido |
| `EstadoBorrarNavegacion` | Guía para borrar caché y datos de navegación |
| `EstadoPasosInicioSesion` | Pasos detallados de inicio de sesión |
| `EstadoPortalBeneficios` | Información sobre el portal de beneficios |
| `EstadoConsultaAdicional` | Pregunta si el usuario necesita más ayuda |
| `EstadoSoporte` | Estado terminal — deriva a agente humano |

### Diagrama de flujo principal

```
[Usuario envía mensaje]
         │
         ▼
  EstadoInicial (List Message)
  ┌──────────────────────────────────┐
  │  • No me puedo registrar         │
  │  • No veo precios                │
  │  • No veo descuentos             │
  │  • Info de mi pedido             │
  └──────────────────────────────────┘
         │
         ▼
  EstadoPreFlujo (mensaje de empatía)
         │
         ▼ [botón CONTINUAR]
    ┌────┴─────────────────────────┐
    │                              │
    ▼                              ▼
EstadoPedirMail             EstadoInfoPedido
(pide email)                      │
    │                              ▼
    ▼                        EstadoSoporte
EstadoLogin / EstadoFormulario    (handoff)
    │
    ▼
EstadoBorrarNavegacion / EstadoPasosInicioSesion
    │
    ▼
EstadoConsultaAdicional
    │
    ├── [¿algo más?] ──► EstadoInicial
    └── [no, gracias] ──► fin de sesión
```

### Tipos de mensajes interactivos

El bot utiliza dos tipos de mensajes interactivos de WhatsApp:

**Reply Buttons** — hasta 3 botones, título máximo 20 caracteres:
```python
Button("continuar", "CONTINUAR")
Button("funciono", "Sí, funcionó")
Button("no_funciono", "No funcionó")
```

**List Messages** — hasta 10 filas por sección, título máximo 24 caracteres, descripción máximo 72:
```python
ListRow("registro", "No me puedo registrar", "Problemas con el registro")
```

### Fallback a agente humano (handoff)

Cuando el bot no puede resolver el problema, activa el modo `human_handoff`. En este modo:

1. Los mensajes del usuario se guardan en el log pero no son procesados por el bot.
2. El agente puede responder usando `POST /agent/send`.
3. El bot puede reanudarse usando `POST /resume`.

---

## BigQuery

### Tablas

#### `chat_history` — Historial de conversaciones

Se crea automáticamente al iniciar si `BIGQUERY_CHAT_LOG_ENABLED=true`.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `timestamp` | TIMESTAMP | Fecha y hora del intercambio (UTC) |
| `session_id` | STRING | ID de sesión (ej: `wa:5491112345678`) |
| `question` | STRING | Mensaje del usuario |
| `answer` | STRING | Respuesta del bot |
| `channel` | STRING | Canal: `whatsapp` o `api` |
| `environment` | STRING | Entorno: `cloud_run` o `local` |

#### `bot_messages` — Mensajes editables *(próximo)*

Almacena los textos, botones y listas del bot para poder editarlos sin tocar el código.

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `message_key` | STRING | Clave única del mensaje |
| `message_type` | STRING | Tipo: `text`, `button`, `list_row_title`, etc. |
| `state_name` | STRING | Estado al que pertenece (nullable para constantes globales) |
| `label` | STRING | Etiqueta legible para el editor |
| `content` | STRING | Contenido actual |
| `default_content` | STRING | Contenido original (para restaurar) |
| `updated_at` | TIMESTAMP | Última modificación (UTC) |
| `updated_by` | STRING | Usuario que realizó el cambio |

### Consulta de ejemplo — logs de las últimas 24 horas

```sql
SELECT
  timestamp,
  session_id,
  question,
  answer,
  channel
FROM `tu-proyecto.inspectia_logs.chat_history`
WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
ORDER BY timestamp DESC
```

---

## Frontend de prueba (Streamlit)

`streamlit_app.py` simula la interfaz de WhatsApp para probar los flujos del bot sin necesidad de usar un teléfono real.

### Características actuales

- **Simulación de chat** — muestra los botones del bot como botones clickeables de Streamlit
- **Reset de sesión** — reinicia la conversación desde cero
- **Panel de info** — muestra el estado actual de la sesión, estados del flujo y health check

### Próximas funcionalidades

- **Descarga de conversaciones** — selección de rango de fechas y exportación en CSV o JSON
- **Editor de mensajes** — edición de textos, botones y listas del bot directamente desde el frontend
- **Editor de botones** — con validación del límite de 20 caracteres de WhatsApp
- **Editor de listas** — con validación de 24/72 caracteres por fila

### Configuración del frontend

En `streamlit_app.py`, la URL de la API se configura con:

```python
API_URL = st.secrets.get("API_URL", "http://localhost:8000")
```

Para producción, configurar `API_URL` en los secrets de Streamlit Cloud o en las variables de entorno del servicio.

---

## Deploy en Google Cloud Run

### 1. Construir y subir la imagen

```bash
gcloud builds submit --tag gcr.io/TU_PROYECTO/btn-bot
```

### 2. Deployar el servicio de la API

```bash
gcloud run deploy btn-bot-api \
  --image gcr.io/TU_PROYECTO/btn-bot \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars CLOUD_RUN=true,BIGQUERY_CHAT_LOG_ENABLED=true,GOOGLE_CLOUD_PROJECT=TU_PROYECTO \
  --set-secrets WHATSAPP_TOKEN=WHATSAPP_TOKEN:latest,WHATSAPP_PHONE_NUMBER_ID=WHATSAPP_PHONE_NUMBER_ID:latest,WHATSAPP_VERIFY_TOKEN=WHATSAPP_VERIFY_TOKEN:latest
```

### 3. Deployar el frontend de Streamlit

```bash
gcloud run deploy btn-bot-streamlit \
  --image gcr.io/TU_PROYECTO/btn-bot \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --command streamlit \
  --args run,streamlit_app.py,--server.port=8080,--server.address=0.0.0.0 \
  --set-env-vars API_URL=https://btn-bot-api-HASH-uc.a.run.app
```

### 4. Configurar el webhook de WhatsApp

En el panel de Meta for Developers:
1. Ir a **WhatsApp > Configuración > Webhooks**
2. URL de callback: `https://btn-bot-api-HASH-uc.a.run.app/webhook`
3. Token de verificación: el valor de `WHATSAPP_VERIFY_TOKEN`
4. Suscribirse al campo `messages`

---

## Roadmap

### En curso

- [ ] **HU-01** — Eliminar mensaje de Hot Sale del `WELCOME_MESSAGE`
- [ ] **HU-02** — Endpoint `GET /chatlog/download` con filtro por rango de fechas
- [ ] **HU-03** — Sección de descarga en el frontend de Streamlit

### Próximo

- [ ] **HU-04** — Tabla `bot_messages` en BigQuery para mensajes editables
- [ ] **HU-08** — Módulo `message_store.py` con cache en memoria (TTL 5 min, thread-safe)
- [ ] **HU-09** — Endpoints `GET/PUT /messages` y `POST /messages/{key}/reset`
- [ ] **HU-05** — Editor de textos del bot en Streamlit
- [ ] **HU-06** — Editor de botones del bot en Streamlit (validación 20 chars)
- [ ] **HU-07** — Editor de listas del bot en Streamlit (validación 24/72 chars)

### Futuro

- [ ] Autenticación en el panel de administración
- [ ] Métricas de uso por estado de flujo
- [ ] Soporte para múltiples idiomas
- [ ] Tests automatizados de los flujos

---

## Licencia

Uso interno — Motorola / Inspectia. No distribuir.
