# AGENTS.md

## Quick start

Python 3.13. Dependencias reproducibles en `requirements.lock` / `requirements-dev.lock` (hashes). Leer `docs/SEGURIDAD.md` antes de cambiar rutas, precios o procesamiento.

```bash
python -m venv .venv && source .venv/bin/activate
uv pip sync requirements-dev.lock --require-hashes
cp .env.example .env  # add at least one API key
uvicorn app.main:app --host 127.0.0.1 --reload --port 8005   # port 8000 is the document-service
```


## Seguridad vigente (prevalece sobre las notas históricas de abajo)

- Los túneles ejecutan `app.main:public_app`: únicamente `/health` y webhooks. La API `app.main:app` usa Bearer: ADMIN_API_KEY y API_CLIENT_KEYS. Ninguna clave vacía habilita acceso. Usar un puerto privado adicional para administración, por ejemplo 8005.
- App Secret/HMAC de Meta y secreto de Telegram son obligatorios. Instagram requiere INSTAGRAM_APP_SECRET y sigue sin enviar respuestas.
- `app/schemas/` limita y valida entradas, rechaza NaN/Infinity. `app/services/catalog.py` aplica precios de `data/quote_catalog.json`; el LLM no fija precios. Personalizados/desconocidos requieren confirmación humana.
- `app/persistence/` y `app/workers/inbox.py` reemplazan dedup JSON/sets: recepción durable antes de 200, leases, reintentos y checkpoints de borrador/respuesta; el worker de lifespan procesa con BOT_CHANNEL. `request_id` es estable por trabajo.
- Historial y último PDF se persisten con TTL acotado, y el acceso a sesiones se serializa con leases SQLite. APIs y canales usan espacios de identidad separados. Transporte devuelve bool; solo confirmar aceptación tras éxito.
- No prometer entrega futura cuando falla el servicio: mensaje de error claro. Un crash tras aceptación externa y antes de checkpoint puede duplicar envíos; no prometer exactamente una vez.
- Límites HTTP/archivo/LLM, cuotas globales, parser PDF aislado y logs redactados se describen en docs/SEGURIDAD.md. No abrir servicios externos ni descargar embeddings en tests unitarios.
- Suite completa: `.venv/Scripts/python.exe -m unittest discover -s tests -v` en Windows; `.venv/bin/python` en Linux. Usar bases temporales y clientes HTTP mockeados.

## Architecture

```text
app/
  main.py                 # Fábrica de aplicaciones y ciclo de vida
  api/                    # Rutas de chat, documentos, webhooks y health
  core/                   # Configuración, seguridad y recursos compartidos
  schemas/                # Contratos Pydantic de entrada/salida
  services/               # Chat, proveedores LLM, cotizaciones, sesiones y costes
  integrations/
    channels/             # Telegram, WhatsApp, Messenger e Instagram
    documents.py          # Cliente del servicio de PDFs
    email.py              # Cliente del servicio de correos
  rag/                    # Ingesta, embeddings y proceso aislado del parser PDF
  persistence/            # SQLite: trabajos, sesiones, cuotas, métricas e índice
  workers/                # Consumo de inbox y cierre por inactividad
  cli/                    # Informe de costes ejecutable como módulo
scripts/                  # Arranque por canal y lectura segura de dotenv
docs/                     # Seguridad, planes y documentación técnica
data/                     # Documentos y catálogo aprobados del negocio
runtime/                  # Bases, logs y PID locales; excluidos de Git
tests/                    # Pruebas por área, siguiendo los paquetes de app
.github/workflows/        # CI, auditoría de dependencias y secretos
```

`docs/PLAN_MULTITENANT.md` es un plan **no implementado** (multitenencia por número de WhatsApp); el código actual es monocliente con `TENANT_ID` configurable, compartido por métricas y documentos. No asumas que existe lógica de tenants.

## Key gotchas

- **Tres claves de servicios distintas, no las confundas**: `INTERNAL_API_KEY` (document-service, cabecera `X-Internal-API-Key`), `EMAIL_SERVICE_API_KEY` (email-service, cabecera `X-API-Key`) y las de los proveedores LLM. La API key de Resend **nunca** sale del email-service: el chatbot no la necesita. El remitente de los correos lo pone el email-service desde su `EMAIL_FROM`; mandar un `from` da 422 `INVALID_REQUEST` porque el esquema usa `extra="forbid"`.
- **`Idempotency-Key` es obligatoria en la práctica**: el email-service nunca la genera por su cuenta. Sin ella, un reintento por timeout **reenvía el correo** y el cliente lo recibe duplicado. Usa `build_idempotency_key(scope, entity_id)` con un id estable del envío (`quote-email/quote-001`), misma clave para el mismo envío y distinta para cada uno.
- **La clave de idempotencia caduca a las 24 h en Resend y el cuerpo manda**: reutilizar la misma clave con un cuerpo **distinto** devuelve `409 invalid_idempotent_request` → el email-service lo traduce a `502 RESEND_SEND_FAILED`, y `send_email` devuelve `None` con un warning. Clave+cuerpo idénticos → devuelve el mismo `email_id` sin reenviar.
- `send_email` devuelve `None` (con `logger.warning`) ante falta de configuración, error de red, estado != 200 o respuesta sin `email_id`; nunca propaga excepciones, igual que `create_quote`. El email va en **Base64 en memoria**: `encode_attachment(filename, bytes, content_type)`, sin ficheros temporales. Límites del servicio: 10 adjuntos, 10 MB cada uno, 25 MB en total.
- **`email_id` va en el nivel superior** de la respuesta, no dentro de `data`: el contrato es `{"success": true, "tenant_id": ..., "email_id": ...}` plano. El envelope de error usa la misma forma, así que hay que comprobar `success` antes de leer el id.
- **Embedding model downloads on first use** (~226 MB to `~/.cache/huggingface`). Needs internet once, then runs fully offline.
- **Índice vectorial**: `rag.sqlite3` (gitignored), SQLite y coseno exacto, hasta 20 000 fragmentos. ChromaDB se retiró por avisos sin parche. No borrar bases antiguas: reindexar originales con backup explícito.
- **E5 embedding prefixes** matter: documents are prefixed `passage: `, queries are prefixed `query: ` (see `QUERY_PREFIX`/`PASSAGE_PREFIX`, `app/rag/embeddings.py`). Mixing them up breaks retrieval quality.
- **LLM provider fallback**: Groq → OpenRouter → Gemini, automatic. Models are hardcoded in `PROVIDERS` (`app/services/llm.py`). At least one `*_API_KEY` in `.env` is required or `/chat` returns 503.
- **Métricas de uso (Fases 0 y 2), sin costeo**: cada respuesta del LLM registra un evento en `usage_events` (`app/persistence/metrics.py`, SQLite stdlib, BD runtime `metrics.sqlite3` gitignored): `event_type="chat_reply"` en `/chat` y `event_type="quote_extraction"` por cada llamada real de `extract_quote_with` (los dos intentos son eventos independientes). `try_provider` devuelve un `LLMResult` con `prompt_tokens`/`completion_tokens`/`total_tokens` leídos de `completion.usage` del proveedor que respondió; si el proveedor no reporta usage quedan en `NULL` (nunca se estiman a mano). Los eventos usan `TENANT_ID` (env, default `"chatbot"`, `app/main.py`), el canal se deduce del prefijo de `conversation_id` (`channel_from_conversation`) y `metadata` no lleva nada sensible (sin keys, sin mensajes del usuario). Best-effort: si SQLite falla solo hay `logger.error` y el bot sigue igual. No tocar modelo/orden/fallback al editar: solo se mide.
- **Eventos de canal WhatsApp (Fase 2), también sin costeo**: `app/integrations/channels/whatsapp.py` registra `message_received` (entrantes) y `message_sent` (salientes) en `usage_events` con `channel="whatsapp"`, `direction`, `wamid`, `contact_id` (el `wa_id` del cliente), `phone_number_id` (entrante: el que trae el payload de Meta; saliente: env `WHATSAPP_PHONE_NUMBER_ID`) y `payload_size` (entrante: tamaño del body crudo del webhook; saliente: bytes del JSON enviado). El `event_time` es uno solo: para entrantes es el timestamp de Meta convertido a ISO 8601 UTC (los retries viejos no llegan por el filtro `STALE_MESSAGE_SECONDS`), para salientes el momento de confirmación. `message_received` se registra en el webhook SOLO después del dedup por wamid (un wamid duplicado jamás genera un segundo evento); `message_sent` se registra al confirmar la Graph API (200 con `messages[0].id`; si Meta no devuelve wamid queda `NULL` con warning; ante un error de Meta no se registra nada). El `media_id` de la subida NO es el wamid (`send_document` registra el wamid del envío, `app/integrations/channels/whatsapp.py`). `metadata` solo lleva `{"message_type": "text"|"document"}`. Los `statuses[]` (delivered/read/failed) NO se registran en `usage_events`: se procesan aparte a `whatsapp_message_status` (ver Fase 3).
- **Costeo y rentabilidad (Fase 3)**: `app/services/costs.py` calcula el coste a partir de `usage_events` (IA) y `whatsapp_message_status` (WhatsApp) usando la tabla `tariffs`; `app/cli/cost_report.py --period YYYY-MM` imprime el informe (coste IA, coste WA, ingreso, beneficio, margen) por tenant. Reglas: (1) solo se factura IA cuando hay **ambos** `prompt_tokens` y `completion_tokens` no-NULL (si falta uno → evento "unknown", excluido del total pero contado, nunca se asume 0); (2) solo se factura un mensaje WhatsApp con `delivered=1` **y** `pricing_billable=1` (una sola categoría de precio, sin doble conteo con `message_sent`); (3) `pricing` ausente → "unknown pricing" (excluido); (4) la categoría `service` tiene tier gratis de 1,000/mes por `(tenant_id, phone_number_id)` (`FREE_WHATSAPP_SERVICE_PER_MONTH`), las demás no; (5) sin ingreso configurado en `monthly_revenue` no se calcula margen. `usage_events` sigue siendo append-only. `seed_default_tariffs()` es insert-only: para cambiar un precio se edita/borra la fila en la BD (o se cambia el default y se limpia). Los precios Meta se tomaron del rate card oficial (Rest of Latin America, USD; RD +1 809/829/849 no es mercado standalone) y **deben re-verificarse contra el CSV oficial** antes de uso comercial; la tarifa de OpenRouter tiene una nota "VERIFICAR" por una discrepancia página/API. `WHATSAPP_MARKET` (env, default `rest_of_latam`) elige el mercado.
- **Nota histórica (comportamiento eliminado)**: `app/rag/indexing.py` abre SQLite perezosamente; una incompatibilidad o error NO borra el índice. La migración/reindexación requiere backup y procedimiento explícito.
- **Side effects at import**: `app/core/config.py` carga el `.env` de la raíz y `app/services/llm.py` construye `PROVIDERS` desde el entorno; `app/rag/indexing.py` no abre SQLite ni descarga modelos al importar. Changes to `.env`, chunk sizes, or collection metadata require a full process restart (no hot reload).
- **Cotizaciones = archivo adjunto, nunca texto**: si un mensaje pide una cotización, los webhooks extraen la estructura con el LLM (`extract_quote_draft`), generan el PDF en el document-service, lo descargan (`fetch_quote_pdf`) y lo envían con `send_document` (WhatsApp sube media a la Graph API → mensaje tipo `document`; Telegram usa `sendDocument`). El system prompt prohíbe al LLM emitir PDFs en base64. Si la extracción falla, el mensaje cae al `/chat` normal.
- **Fallos de cotización silenciosos**: sin `DOCUMENT_SERVICE_URL` o `INTERNAL_API_KEY` en `.env`, o si el document-service devuelve error / el PDF no se descarga, `materialize_quote` devuelve `None` y el bot responde `QUOTE_PENDING_MSG` (texto genérico) — no se manda ningún PDF y la única pista es un `logger.warning`. Si "el PDF no llega", revisa esos logs antes de tocar código.
- **Cotización sin ítems determinables → preguntar, nunca prometer**: si la extracción devuelve `items: []` (p. ej. un pedido genérico como "cotillón para pasado mañana" porque el precio del cotillón varía según tamaño e invitados), el bot responde con `QUOTE_CLARIFY_MSG` pidiendo productos/cantidades, nombre y fecha, y marca la conversación en `pending_quote_info`. La siguiente respuesta del cliente se extrae con `force_quote=True` (intent obligado a "quote") aunque no traiga keywords. El prompt de extracción recibe la fecha de hoy (`{today}`) para calcular fechas relativas como "pasado mañana" y las pone en `event_date`; como la plantilla del document-service **no tiene campo de fecha**, la fecha/nº de personas se inyectan en la descripción del primer ítem y el título, y los ítems con `unit_price: 0.0` se marcan como "precios a confirmar según tamaño" en el caption.
- **Reenvío determinista + cierre social**: si la conversación ya recibió un PDF (`_last_quote_pdf`), mensajes como "envíame el archivo pdf", "pásame el pdf" o "mándamela" reenvían **ese** PDF almacenado sin llamar al LLM (`is_pure_resend`; el gate de cotización incluye `pdf`/`archivo`/`adjunto`/`reenvi`). Mensajes con datos de cotización nueva (personas, precios, "para...", etc.) NO se consideran reenvío puro y van a la extracción. Los "gracias"/cierres puros responden con `SOCIAL_CLOSE_MSG` (una línea, sin LLM) y limpian `pending_quote_info`.
- **Cierre automático por inactividad (una sola etapa)**: tras la última respuesta del bot, si el cliente no escribe en `IDLE_CLOSE_SECONDS` (env, default 600 = 10 min), un `_idle_monitor` (arrancado vía `lifespan`, revisa cada `IDLE_CHECK_INTERVAL`) envía `CLOSE_FINAL_MSG` y llamada `_close_conversation` limpia memoria, cotizaciones y PDFs. `_track_idle`/`_clear_idle` (`app/main.py`) registran el contador por `conversation_id`; el monitor decide el canal por el prefijo del `conversation_id` (`whatsapp:`/`messenger:`/`telegram:` — `app/main.py`) y un canal desconocido solo loguea un warning sin enviar nada.
- **Estado y dedup**: historial/PDFs tienen caché en memoria y persistencia SQLite con TTL. Inbox SQLite conserva IDs de dedup siete días y recupera trabajos mediante leases. No se usan logs/recent_wamids.json ni logs/recent_mids.json.
- **Entregas viejas de Meta se descartan**: los `parse_messages` de WhatsApp, Messenger e Instagram ignoran mensajes cuyo `timestamp` supere `STALE_MESSAGE_SECONDS` (900 s ≈ 15 min; los tres adapters) — son retries de Meta de mensajes ya respondidos; sin esto el bot "habla solo" tras una caída/lentitud prolongada del proceso. Messenger envía el timestamp en **milisegundos** y lo divide a 1000 antes de comparar.

## Run a single test / verification

Suite unittest con SQLite real temporal, HTTP/LLM mockeados, parser PDF aislado y worker de lifespan:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Informe de costes/rentabilidad de un período (siembra tarifas por defecto salvo `--no-seed`):

```bash
.venv/bin/python -m app.cli.cost_report --period 2026-10
```

End-to-end manual verification:

```bash
# Start server (default port 8001; 8000 is the document-service)
uvicorn app.main:app --host 127.0.0.1 --port 8005

# Index sample data
curl -X POST http://127.0.0.1:8005/documents/upload -H "Authorization: Bearer $ADMIN_API_KEY" -F "file=@data/business_data.txt"

# Chat (registra un evento usage_events chat_reply en metrics.sqlite3)
curl -X POST http://127.0.0.1:8005/chat -H "Authorization: Bearer $ADMIN_API_KEY" -H "Content-Type: application/json" -d '{"message": "test"}'

# Health check (shows configured providers)
curl http://127.0.0.1:8005/health
```

To inspect recorded events:

```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect(str(__import__('app.core.config', fromlist=['METRICS_DB_PATH']).METRICS_DB_PATH)); print(c.execute('select event_type, provider, model, total_tokens, tenant_id from usage_events order by id').fetchall())"
```

## Language

Code comments, README, and user-facing strings are in Spanish. Maintain this convention.

## Telegram bot

`scripts/run_bot.sh` starts uvicorn + a Cloudflare quick tunnel and registers the webhook. Subcommands: `start` (default), `stop`, `webhook` (re-register). Requires `TELEGRAM_BOT_TOKEN` in `.env` (it auto-generates `TELEGRAM_WEBHOOK_SECRET` if missing) and `cloudflared` in PATH or as `./cloudflared`. The free `trycloudflare.com` URL changes each restart, so `webhook` must be re-registered after a tunnel restart.

- `/webhooks/telegram` procesa el mensaje en un inbox durable por `update_id` y confirma 200 tras persistir el trabajo para no responder dos veces si Telegram reintenta. Si `/chat` falla (503), el error se registra y el webhook igual responde 200 — Telegram no reintenta un update ya confirmado.

## WhatsApp bot (Meta Cloud API)

- `scripts/run_whatsapp.sh` starts uvicorn on port 8002 + a tunnel and **prints** the URL: Meta does NOT allow API webhook registration, the URL + verify token must be pasted manually in the dev dashboard (App → WhatsApp → Configuration → Webhook, and subscribe to `messages`). After a tunnel restart the URL must be re-pasted. Also needs `cloudflared` in PATH or as `./cloudflared`.
- **Each run script only kills its own processes**: they write channel-specific pids (`runtime/logs/telegram_uvicorn.pid` + `telegram_cloudflared.pid` vs `runtime/logs/whatsapp_uvicorn.pid` + `whatsapp_cloudflared.pid` vs `runtime/logs/messenger_uvicorn.pid` + `messenger_cloudflared.pid`, see `scripts/run_bot.sh` / `scripts/run_whatsapp.sh` / `scripts/run_messenger.sh`). `stop` from one no longer kills the others. Ports are fixed: telegram bot on **8001** (`scripts/run_bot.sh`), whatsapp bot on **8002**, messenger bot on **8004** (`scripts/run_messenger.sh`), document-service on **8000** — never run the bot on 8000, that's the document-service.
- All three run scripts skip launching uvicorn if `http://127.0.0.1:$PORT/health` already responds **with `"providers_configured"`** (`scripts/run_bot.sh` / `scripts/run_whatsapp.sh` / `scripts/run_messenger.sh`) — any other service answering on that port is not reused as the bot.
- **Three unrelated secrets**: `WHATSAPP_VERIFY_TOKEN` (handshake only), `WHATSAPP_APP_SECRET` (HMAC signature), `WHATSAPP_ACCESS_TOKEN` (send API).
- The `X-Hub-Signature-256` is HMAC-SHA256 over the **raw request body** using the app secret — `app/main.py` reads `await request.body()` and never re-serializes the parsed JSON to verify (see `verify_signature` in `app/integrations/channels/whatsapp.py`).
- GET `/webhooks/whatsapp` must echo `hub.challenge` as **plain text** (no JSON) or the dashboard verification fails.
- **24h service window**: free-form replies only work within 24h of the customer's last message; outside that window only approved templates are accepted (`send_message` failures are just logged).
- Meta retries failed deliveries → `app/main.py` deduplica por `wamid` en inbox SQLite y procesa cada mensaje en inbox SQLite antes de confirmar 200.
- Incoming text only: non-text types (media, interactive, buttons) are logged and ignored in `parse_messages`.
- Only `type: text` messages from `entry[].changes[].value.messages[]` are handled; `conversation_id` is `whatsapp:<wa_id>`.

## Messenger bot (Meta Graph API)

- `scripts/run_messenger.sh` starts uvicorn on port 8004 + a tunnel and **prints** the URL: igual que WhatsApp, Meta no permite registrar el webhook por API — la URL + verify token se pegan a mano en el panel (App → Messenger → Configuration → Webhook, suscribirse a `messages`). Tras reiniciar el túnel hay que re-pegarla. Requiere `cloudflared` en PATH o como `./cloudflared`.
- **Secrets**: `MESSENGER_VERIFY_TOKEN` (handshake, se responde el challenge en **texto plano**), `MESSENGER_APP_SECRET` (firma HMAC), `MESSENGER_ACCESS_TOKEN` (send API), `MESSENGER_PAGE_ID` (detectar ecos).
- La firma `X-Hub-Signature-256` se verifica sobre el **body crudo** (`app/main.py`), igual que WhatsApp.
- **Echoes descartados**: los mensajes que envió la propia Página llegan como eco — `parse_messages` los ignora por `message.is_echo`, por `sender.id == MESSENGER_PAGE_ID` y por el array `message_echoes` (`app/integrations/channels/messenger.py`; Meta no garantiza que vengan los tres, por eso se comprueban todos). Sin esto el bot se responde a sí mismo en bucle.
- Dedup por `mid` en inbox SQLite; worker común de canal con leases, checkpoints y reintentos.
- **Solo texto**: adjuntos/stickers/quick replies se loguean y se ignoran. El texto de respuesta se limpia de Markdown (`_format_for_messenger`) y se trocea en mensajes de ≤ 2000 chars (`_split_text`, corta en párrafos → líneas → espacios).
- `conversation_id` es `messenger:<psid>`; el **psid no es un teléfono**, así que el flujo de cotización pasa `fallback_phone=""` (`app/main.py`).
- `parse_messages` descarta timestamps con más de 900 s (el timestamp llega en **milisegundos**).

## Instagram (solo recepción)

- `GET/POST /webhooks/instagram` existe y valida el handshake con `INSTAGRAM_VERIFY_TOKEN`, pero el POST **solo parsea y loguea** los mensajes (`app/main.py`) — `app/integrations/channels/instagram.py` no tiene función de envío y el flujo de cotización no está conectado. No respondas por este canal hasta que se implemente el envío.

## Organización y rutas

Leer `docs/ESTRUCTURA.md`. Rutas HTTP en `app/api`; lógica de negocio en `app/services`; llamadas externas en `app/integrations`; consultas SQLite en `app/persistence`. Los workers coordinan persistencia y servicios. No añadir lógica de chat a `app/main.py` ni imports de las rutas HTTP desde servicios. Usar imports absolutos `app.*`. Las rutas de datos y bases se resuelven en `app/core/config.py` desde la raíz, y `runtime_path` conserva bases antiguas existentes cuando no hay una variable explícita. No mover bases automáticamente. Tests por área bajo `tests/`, sin modificar `sys.path`.
