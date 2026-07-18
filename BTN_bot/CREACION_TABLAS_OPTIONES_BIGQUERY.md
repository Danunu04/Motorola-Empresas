# Creación de las tablas de opciones en BigQuery

## Objetivo

Esta guía explica cómo aparecen y se inicializan las tablas `option_bindings` y `option_group_config`, qué necesita el backend para crearlas y qué debe verificarse antes del primer arranque conectado a BigQuery.

No es necesario crear estas tablas manualmente en el caso normal. El backend contiene la creación de esquema y el seed inicial. Sí es necesario preparar el proyecto, las credenciales y los permisos de Google Cloud.

## Respuesta corta

Con BigQuery correctamente configurado, el primer arranque del backend hace lo siguiente:

1. Crea el dataset configurado si todavía no existe.
2. Crea `mensajes_editables` si todavía no existe.
3. Crea `option_bindings` y `option_group_config` si todavía no existen.
4. Inserta los defaults faltantes: 34 opciones distribuidas en 17 grupos y una configuración por cada grupo.
5. Carga en caché el estado actual para que el bot pueda renderizar y enrutar las opciones.

Las llamadas de creación usan `exists_ok=True`, por lo que reiniciar el backend no vuelve a crear ni borra una tabla que ya existe.

## Cuándo se ejecuta la creación automática

El punto de entrada es el evento `startup` de FastAPI en `bot.py`:

```text
startup de FastAPI
  -> message_store.load_all_messages()
  -> inicialización del cliente de BigQuery
  -> _ensure_table()
  -> _ensure_option_tables()
  -> _seed_option_tables()
```

El cliente se inicializa una sola vez por proceso. Por eso la creación normalmente sucede durante el arranque, al cargar el catálogo de mensajes. Si esa carga no hubiera inicializado el cliente, también puede dispararse al efectuar la primera lectura o escritura que necesite BigQuery.

## Requisitos previos

El proceso que ejecuta el backend necesita:

- La dependencia `google-cloud-bigquery` instalada.
- La API de BigQuery habilitada en el proyecto de Google Cloud.
- `GOOGLE_CLOUD_PROJECT` definido.
- Credenciales válidas mediante Application Default Credentials (ADC) o la identidad del servicio donde se despliega.
- Permisos para crear el dataset si no existe, crear tablas, ejecutar consultas e insertar filas.
- Si existe una migración legacy, permiso para eliminar la tabla `bot_messages` después de copiarla.

Las credenciales no se suben al repositorio. En desarrollo pueden obtenerse mediante ADC o un archivo de cuenta de servicio fuera del repositorio. En Cloud Run se recomienda asociar una cuenta de servicio al servicio, sin distribuir un JSON.

## Variables de entorno

| Variable | Obligatoria | Valor por defecto | Uso |
|---|---:|---|---|
| `GOOGLE_CLOUD_PROJECT` | Sí, para activar BigQuery | Sin valor | Proyecto donde se crean dataset y tablas. |
| `BIGQUERY_CHAT_LOG_DATASET` | No | `inspectia_logs` | Dataset que contiene mensajes y opciones. |
| `BIGQUERY_BOT_MESSAGES_TABLE` | No | `mensajes_editables` | Tabla del catálogo de mensajes. |
| `BIGQUERY_OPTION_BINDINGS_TABLE` | No | `option_bindings` | Tabla versionada de opciones. |
| `BIGQUERY_OPTION_GROUP_CONFIG_TABLE` | No | `option_group_config` | Tabla versionada de presentación por grupo. |
| `BIGQUERY_CLOUD_LOCATION` | No | `us-central1` | Ubicación usada al crear el dataset. |

`BIGQUERY_CHAT_LOG_ENABLED` controla el subsistema de chat logs, pero no desactiva `message_store`. Para mensajes y opciones, la conexión se considera configurada cuando existe la librería y `GOOGLE_CLOUD_PROJECT` tiene valor.

### Particularidad actual de `.env`

Actualmente `bot.py` importa `message_store` antes de ejecutar `load_dotenv()`. Como los nombres del dataset y las tablas se leen al importar `message_store`, una personalización definida únicamente en `.env` puede llegar demasiado tarde y quedar reemplazada por los valores por defecto.

Hasta corregir ese orden de importación, las variables que cambien nombres o ubicación deben definirse en el entorno del proceso o del contenedor antes de iniciar Python. `GOOGLE_CLOUD_PROJECT` sí se consulta al crear el cliente, pero por coherencia operativa también conviene inyectarlo antes del arranque.

## Esquema creado para `option_bindings`

| Campo | Tipo | Modo / default | Función |
|---|---|---|---|
| `option_group` | STRING | REQUIRED | Grupo al que pertenece la opción. |
| `option_id` | STRING | REQUIRED | ID estable transportado por la interacción. |
| `orden` | INTEGER | REQUIRED | Orden dentro del grupo. |
| `title_key` | STRING | REQUIRED | Clave del título de lista en `mensajes_editables`. |
| `button_title_key` | STRING | NULLABLE | Clave alternativa para el título corto de botón. |
| `description_key` | STRING | NULLABLE | Clave de descripción de fila de lista. |
| `target_state` | STRING | NULLABLE | Estado destino de un salto. |
| `target_vars` | STRING | NULLABLE | Objeto JSON serializado con variables a aplicar. |
| `stay_in_state` | BOOLEAN | DEFAULT FALSE | Indica que la opción cambia un substep sin saltar de estado. |
| `target_substep_key` | STRING | NULLABLE | Variable de substep que debe modificarse. |
| `target_substep_value` | STRING | NULLABLE | Nuevo valor del substep. |
| `reply_key` | STRING | NULLABLE | Clave del mensaje que responde al clic. |
| `extra_flags` | STRING | NULLABLE | Objeto JSON serializado para flags adicionales, por ejemplo `handoff`. |
| `deleted` | BOOLEAN | DEFAULT FALSE | Tombstone de eliminación lógica. |
| `updated_at` | TIMESTAMP | REQUIRED | Momento UTC de la versión. |
| `version_id` | STRING | REQUIRED | Identificador único de la versión. |
| `updated_by` | STRING | NULLABLE | Actor que originó el cambio. |

## Esquema creado para `option_group_config`

| Campo | Tipo | Modo / default | Función |
|---|---|---|---|
| `option_group` | STRING | REQUIRED | Grupo configurado. |
| `button_text_key` | STRING | REQUIRED | Clave de `mensajes_editables` para el texto que abre una lista. |
| `section_title_key` | STRING | REQUIRED | Clave para el título de sección de la lista. |
| `deleted` | BOOLEAN | DEFAULT FALSE | Tombstone de eliminación lógica. |
| `updated_at` | TIMESTAMP | REQUIRED | Momento UTC de la versión. |
| `version_id` | STRING | REQUIRED | Identificador único de la versión. |
| `updated_by` | STRING | NULLABLE | Actor que originó el cambio. |

## Qué contiene el seed inicial

El seed incluido en `message_store.py` agrega:

- 34 opciones de `DEFAULT_OPTION_BINDINGS`.
- 17 configuraciones de `DEFAULT_OPTION_GROUP_CONFIGS`.
- Los mensajes de `DEFAULT_MESSAGES` que todavía no tengan historial en `mensajes_editables`.

El seed es idempotente por clave lógica. Antes de insertar, consulta si alguna vez existió la clave:

- En `option_bindings`: `option_group + option_id`.
- En `option_group_config`: `option_group`.
- En `mensajes_editables`: `message_key`.

Una opción eliminada lógicamente conserva historial y no vuelve a aparecer por reiniciar el backend. Esto evita que el seed resucite tombstones.

## Qué ocurre si las tablas ya existen

Si una tabla ya existe con el esquema correcto, se reutiliza y solo se insertan defaults cuya clave nunca apareció en su historial.

`exists_ok=True` no es una herramienta de migración de esquema. Si una tabla fue creada antes con campos faltantes, tipos incorrectos o restricciones incompatibles, el backend no la modifica automáticamente. En ese caso se debe aplicar una migración controlada o crear una tabla correcta y copiar los datos antes de habilitar el backend.

Por este motivo, una creación manual no aporta ventajas salvo que la política de infraestructura impida que la identidad de runtime cree datasets o tablas. Si se crean mediante Terraform, consola o SQL, sus nombres y esquemas deben coincidir exactamente con los definidos por el backend.

## Persistencia append-only

Las escrituras normales de opciones y configuraciones no ejecutan `UPDATE` ni `DELETE` sobre sus filas. Cada cambio usa streaming con `insert_rows_json()` y crea una versión nueva con `updated_at` y `version_id` nuevos.

La lectura obtiene la última versión de cada clave mediante `QUALIFY ROW_NUMBER()` y excluye las filas cuya última versión tiene `deleted = TRUE`.

La compactación futura podrá eliminar mediante DML versiones antiguas y tombstones. No es necesaria para que el sistema funcione y no forma parte del arranque ni de la creación de tablas.

## Advertencia sobre la migración legacy

En el primer arranque conectado, `_ensure_table()` también busca una tabla llamada `bot_messages`:

1. Lee la versión más reciente de cada `message_key`.
2. Inserta esas versiones en `mensajes_editables`.
3. Si la copia finaliza sin errores, elimina físicamente `bot_messages`.
4. Si la tabla legacy está vacía, también la elimina.

Esto es intencional en el código actual, pero es una operación destructiva sobre la tabla legacy. Antes del primer arranque en un proyecto que pueda contener `bot_messages`, se debe revisar su contenido y conservar un respaldo o snapshot si hace falta.

## Desarrollo local y Docker

### Desarrollo local con ADC

El flujo habitual es:

1. Autenticarse localmente con ADC.
2. Definir las variables en el entorno antes de iniciar Python.
3. Levantar FastAPI.

La sesión local de `gcloud` no se guarda en Git. Otro desarrollador que clone el repositorio debe autenticarse con su propia identidad autorizada.

### Contenedor local

El contenedor tampoco recibe credenciales por estar construido desde el repositorio. Se debe elegir uno de estos mecanismos:

- Montar credenciales ADC o un JSON como volumen de solo lectura y definir `GOOGLE_APPLICATION_CREDENTIALS` con una ruta interna al contenedor.
- Ejecutar el contenedor en una plataforma que le proporcione una identidad de servicio.

Las variables deben inyectarse al crear el contenedor. No deben copiarse credenciales dentro de la imagen.

### Cloud Run

En Cloud Run se recomienda:

1. Asociar una cuenta de servicio con los permisos requeridos.
2. Definir proyecto, dataset, ubicación y nombres de tabla como variables del servicio.
3. Desplegar la imagen sin archivos de credenciales.

## Qué sucede si BigQuery no está disponible

El catálogo puede cargar los defaults locales y permitir que el backend termine de iniciar. En ese modo:

- El bot puede leer mensajes y opciones por defecto.
- Las escrituras de mensajes u opciones devuelven HTTP 503.
- La persistencia no está operativa.

Por lo tanto, ver el proceso de FastAPI activo o recibir un `200` del health check no demuestra por sí solo que las tablas fueron creadas o que BigQuery está conectado.

## Verificación segura después de habilitar BigQuery

Esta verificación debe hacerse recién cuando se autorice probar contra el proyecto real:

1. Revisar previamente si existe `bot_messages` y decidir si su eliminación automática es aceptable.
2. Iniciar una única instancia del backend con las variables y credenciales correctas.
3. Confirmar en los logs la creación o reutilización de las tablas y la ejecución del seed.
4. Verificar en BigQuery que existen `mensajes_editables`, `option_bindings` y `option_group_config` con sus esquemas completos.
5. Ejecutar `GET /options/groups` y `GET /options/menu_principal`.
6. Comprobar que las respuestas resueltas informan `source: "bigquery"` y no `source: "default"` donde corresponda.
7. Hacer una escritura controlada desde la API y comprobar que agrega una fila nueva sin modificar versiones anteriores.
8. Reiniciar el backend y verificar que no duplica seeds ni resucita opciones eliminadas.

## Estado actual de la validación

La creación, el seed, la lectura versionada y las escrituras append-only están cubiertos por pruebas locales con clientes de BigQuery simulados. Todavía queda pendiente la prueba de integración contra tablas reales, tal como se acordó para esta etapa.

