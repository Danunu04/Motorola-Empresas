# Pendientes para persistencia en BigQuery

## Contexto

El backend (`BTN_bot/bot.py`) y el editor de mensajes del frontend ya están listos para leer, editar, crear y reordenar mensajes. Sin embargo, en el entorno local no se configuró autenticación ni conexión a BigQuery, por lo que las operaciones de escritura (PUT /messages, POST /messages, PATCH /messages/reorder, POST /messages/{key}/reset) devolverán HTTP 503.

## Qué falta configurar

1. **Proyecto de GCP**
   - Tener acceso a un proyecto de Google Cloud Platform.
   - Asegurarse de que la API de BigQuery esté habilitada.

2. **Dataset y tabla**
   - Dataset: el que se use en `BIGQUERY_CHAT_LOG_DATASET` (default: `inspectia_logs`).
   - Tabla: `mensajes_editables` (definida en `BIGQUERY_BOT_MESSAGES_TABLE`).
   - El backend puede crear la tabla automáticamente mediante `_ensure_table()` si la cuenta de servicio tiene permisos de `bigquery.tables.create`.

3. **Credenciales de servicio**
   - Crear o usar una cuenta de servicio de GCP con al menos estos roles:
     - `BigQuery Data Editor`
     - `BigQuery Job User`
   - Descargar el archivo JSON de la clave.

4. **Variables de entorno**
   - Antes de levantar `bot.py`, exportar:
     ```bash
     export GOOGLE_APPLICATION_CREDENTIALS=/ruta/a/la/clave-service-account.json
     export BIGQUERY_CHAT_LOG_DATASET=inspectia_logs
     export BIGQUERY_BOT_MESSAGES_TABLE=mensajes_editables
     ```
   - Opcional: agregar estas variables a un archivo `.env` en `BTN_bot/` (el backend ya carga `.env` al iniciar).

## Cómo levantar el backend con BigQuery

```bash
cd /Volumes/1TB/Inspectia/Motorola-Empresas/BTN_bot
source .env
python3 -m uvicorn bot:app --host 0.0.0.0 --port 8000 --reload
```

## Cómo migrar datos de la tabla vieja

- En el primer startup con BigQuery configurado, el backend intentará leer la tabla vieja `bot_messages` y copiar sus últimas versiones de cada `message_key` a `mensajes_editables`.
- Luego la tabla vieja se elimina automáticamente (`_drop_legacy_table`).
- Si la tabla vieja no existe, se pre-pobla `mensajes_editables` con los `DEFAULT_MESSAGES` del catálogo.

## Qué probar una vez configurado

1. Editar un mensaje existente en `/editor-mensajes` y verificar que se refleja tras recargar.
2. Crear un mensaje nuevo y confirmar que aparece en el flujo correcto.
3. Reordenar con drag & drop y recargar para validar que el orden persiste.
4. Restaurar un mensaje al contenido por defecto y verificar el cambio.

## Notas

- El frontend actualmente apunta a `/api/messages` a través del proxy de desarrollo (`proxy.conf.json`) hacia `http://localhost:8000`.
- Para producción, la URL del backend debe apuntar al deploy real y el frontend debe compilarse con `npm run build` usando `environment.prod.ts`.
