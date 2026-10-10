# AGENTS.md

## Quick start

Python 3.12+ (developed on 3.14), `requirements.txt` unpinned.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add at least one API key
uvicorn main:app --reload --port 8001   # port 8000 is the document-service
```

## Architecture

Flat Python project, no packages. All source files are in the root:

| File | Role |
|---|---|
| `main.py` | FastAPI app: `/chat`, `/documents/*`, `/health`, `/webhooks/telegram`, `/webhooks/whatsapp`, `/webhooks/messenger`, `/webhooks/instagram`. `/documents/quote` delega en `document_service.py`; los webhooks detectan cotizaciones y envían el PDF como archivo adjunto (`send_document`) |
| `rag.py` | ChromaDB ingestion (chunking, indexing) and retrieval |
| `embeddings.py` | Local multilingual E5-small via ONNX (no API key needed) |
| `telegram_adapter.py` | Telegram Bot API helpers (parse updates, send messages, send documents) |
| `whatsapp_adapter.py` | WhatsApp Cloud API helpers (signature check, parse/send, send documents) |
| `messenger_adapter.py` | Messenger helpers (signature check, echo discard, parse/send text, send documents, chunking 2000 chars) |
| `instagram_adapter.py` | Instagram webhook parser — **solo recepción** (extrae y loguea; no envía respuestas aún) |
| `document_service.py` | Client for the external document-service (`POST /quotes` → PDF, fetch_quote_pdf) |
| `email_service.py` | Client for the external email-service (`POST /api/v1/emails/send` → email_id). **Todavía no lo llama ningún flujo**: queda listo y verificado, sin uso |
| `metrics.py` | Métricas de uso y costeo (Fases 0, 2 y 3): registra eventos en SQLite (tablas `usage_events`, `tariffs`, `monthly_revenue` y `whatsapp_message_status`; BD runtime `metrics.sqlite3`, gitignored, migración no destructiva con `ALTER TABLE... ADD COLUMN` para columnas nuevas). Best-effort: si SQLite falla solo loguea y el bot sigue. Contiene la ESCRITURA; las fórmulas/tarifas por defecto/informe viven en `costs.py` |
| `costs.py` | Costeo (Fase 3): `DEFAULT_TARIFFS` (LLM por 1M tokens; WhatsApp por mensaje), `seed_default_tariffs()` (insert-only, idempotente), `llm_cost_usd(...)`, `_pick_llm_tariff`/`_pick_wa_tariff`, `build_report(period, tenant_id, market)` y `format_report(report)`. Solo usa tarifas de la BD; no estima tokens/precios |
| `cost_report.py` | CLI del informe de costes/rentabilidad: `--period YYYY-MM` (obligatorio), `--tenant`, `--market`, `--db`, `--no-seed`. Siembra tarifas por defecto salvo `--no-seed`; si la BD no existe imprime "sin datos" y sale 0 sin crearla |

`PLAN_MULTITENANT.md` es un plan **no implementado** (multitenencia por número de WhatsApp); el código actual es monocliente con `QUOTE_TENANT_ID = "chatbot"` hardcodeado (`main.py:460`). No asumas que existe lógica de tenants.

## Key gotchas

- **Tres claves de servicios distintas, no las confundas**: `INTERNAL_API_KEY` (document-service, cabecera `X-Internal-API-Key`), `EMAIL_SERVICE_API_KEY` (email-service, cabecera `X-API-Key`) y las de los proveedores LLM. La API key de Resend **nunca** sale del email-service: el chatbot no la necesita. El remitente de los correos lo pone el email-service desde su `EMAIL_FROM`; mandar un `from` da 422 `INVALID_REQUEST` porque el esquema usa `extra="forbid"`.
- **`Idempotency-Key` es obligatoria en la práctica**: el email-service nunca la genera por su cuenta. Sin ella, un reintento por timeout **reenvía el correo** y el cliente lo recibe duplicado. Usa `build_idempotency_key(scope, entity_id)` con un id estable del envío (`quote-email/quote-001`), misma clave para el mismo envío y distinta para cada uno.
- **La clave de idempotencia caduca a las 24 h en Resend y el cuerpo manda**: reutilizar la misma clave con un cuerpo **distinto** devuelve `409 invalid_idempotent_request` → el email-service lo traduce a `502 RESEND_SEND_FAILED`, y `send_email` devuelve `None` con un warning. Clave+cuerpo idénticos → devuelve el mismo `email_id` sin reenviar.
- `send_email` devuelve `None` (con `logger.warning`) ante falta de configuración, error de red, estado != 200 o respuesta sin `email_id`; nunca propaga excepciones, igual que `create_quote`. El email va en **Base64 en memoria**: `encode_attachment(filename, bytes, content_type)`, sin ficheros temporales. Límites del servicio: 10 adjuntos, 10 MB cada uno, 25 MB en total.
- **`email_id` va en el nivel superior** de la respuesta, no dentro de `data`: el contrato es `{"success": true, "tenant_id": ..., "email_id": ...}` plano. El envelope de error usa la misma forma, así que hay que comprobar `success` antes de leer el id.
- **Embedding model downloads on first use** (~226 MB to `~/.cache/huggingface`). Needs internet once, then runs fully offline.
- **ChromaDB vector store** is in `chroma_db/` (gitignored, regenerable). To reset: `rm -rf chroma_db/` then re-upload documents.
- **E5 embedding prefixes** matter: documents are prefixed `passage: `, queries are prefixed `query: ` (see `QUERY_PREFIX`/`PASSAGE_PREFIX`, `embeddings.py:31-32`). Mixing them up breaks retrieval quality.
- **LLM provider fallback**: Groq → OpenRouter → Gemini, automatic. Models are hardcoded in `PROVIDERS` (`main.py:91`). At least one `*_API_KEY` in `.env` is required or `/chat` returns 503.
- **Métricas de uso (Fases 0 y 2), sin costeo**: cada respuesta del LLM registra un evento en `usage_events` (`metrics.py`, SQLite stdlib, BD runtime `metrics.sqlite3` gitignored): `event_type="chat_reply"` en `/chat` y `event_type="quote_extraction"` por cada llamada real de `_extract_quote_with` (los dos intentos son eventos independientes). `_try_provider` devuelve un `LLMResult` con `prompt_tokens`/`completion_tokens`/`total_tokens` leídos de `completion.usage` del proveedor que respondió; si el proveedor no reporta usage quedan en `NULL` (nunca se estiman a mano). Los eventos usan `TENANT_ID` (env, default `"chatbot"`, `main.py:462`), el canal se deduce del prefijo de `conversation_id` (`_channel_from_conversation`) y `metadata` no lleva nada sensible (sin keys, sin mensajes del usuario). Best-effort: si SQLite falla solo hay `logger.error` y el bot sigue igual. No tocar modelo/orden/fallback al editar: solo se mide.
- **Eventos de canal WhatsApp (Fase 2), también sin costeo**: `whatsapp_adapter.py` registra `message_received` (entrantes) y `message_sent` (salientes) en `usage_events` con `channel="whatsapp"`, `direction`, `wamid`, `contact_id` (el `wa_id` del cliente), `phone_number_id` (entrante: el que trae el payload de Meta; saliente: env `WHATSAPP_PHONE_NUMBER_ID`) y `payload_size` (entrante: tamaño del body crudo del webhook; saliente: bytes del JSON enviado). El `event_time` es uno solo: para entrantes es el timestamp de Meta convertido a ISO 8601 UTC (los retries viejos no llegan por el filtro `STALE_MESSAGE_SECONDS`), para salientes el momento de confirmación. `message_received` se registra en el webhook SOLO después del dedup por wamid (un wamid duplicado jamás genera un segundo evento); `message_sent` se registra al confirmar la Graph API (200 con `messages[0].id`; si Meta no devuelve wamid queda `NULL` con warning; ante un error de Meta no se registra nada). El `media_id` de la subida NO es el wamid (`send_document` registra el wamid del envío, `whatsapp_adapter.py:214`). `metadata` solo lleva `{"message_type": "text"|"document"}`. Los `statuses[]` (delivered/read/failed) NO se registran en `usage_events`: se procesan aparte a `whatsapp_message_status` (ver Fase 3).
- **Costeo y rentabilidad (Fase 3)**: `costs.py` calcula el coste a partir de `usage_events` (IA) y `whatsapp_message_status` (WhatsApp) usando la tabla `tariffs`; `cost_report.py --period YYYY-MM` imprime el informe (coste IA, coste WA, ingreso, beneficio, margen) por tenant. Reglas: (1) solo se factura IA cuando hay **ambos** `prompt_tokens` y `completion_tokens` no-NULL (si falta uno → evento "unknown", excluido del total pero contado, nunca se asume 0); (2) solo se factura un mensaje WhatsApp con `delivered=1` **y** `pricing_billable=1` (una sola categoría de precio, sin doble conteo con `message_sent`); (3) `pricing` ausente → "unknown pricing" (excluido); (4) la categoría `service` tiene tier gratis de 1,000/mes por `(tenant_id, phone_number_id)` (`FREE_WHATSAPP_SERVICE_PER_MONTH`), las demás no; (5) sin ingreso configurado en `monthly_revenue` no se calcula margen. `usage_events` sigue siendo append-only. `seed_default_tariffs()` es insert-only: para cambiar un precio se edita/borra la fila en la BD (o se cambia el default y se limpia). Los precios Meta se tomaron del rate card oficial (Rest of Latin America, USD; RD +1 809/829/849 no es mercado standalone) y **deben re-verificarse contra el CSV oficial** antes de uso comercial; la tarifa de OpenRouter tiene una nota "VERIFICAR" por una discrepancia página/API. `WHATSAPP_MARKET` (env, default `rest_of_latam`) elige el mercado.
- **Collection auto-recreation**: `rag.py` checks collection metadata at **import time** (`rag.py:36-67`). If the embedding model or distance metric changed, ChromaDB detects the mismatch and recreates the collection — existing documents must be re-indexed.
- **Side effects at import**: `main.py` calls `load_dotenv()` and builds `PROVIDERS` from env; `rag.py` opens ChromaDB, loads the ONNX model, and runs the recreate check on `import`. Changes to `.env`, chunk sizes, or collection metadata require a full process restart (no hot reload).
- **Cotizaciones = archivo adjunto, nunca texto**: si un mensaje pide una cotización, los webhooks extraen la estructura con el LLM (`_extract_quote_draft`), generan el PDF en el document-service, lo descargan (`fetch_quote_pdf`) y lo envían con `send_document` (WhatsApp sube media a la Graph API → mensaje tipo `document`; Telegram usa `sendDocument`). El system prompt prohíbe al LLM emitir PDFs en base64. Si la extracción falla, el mensaje cae al `/chat` normal.
- **Fallos de cotización silenciosos**: sin `DOCUMENT_SERVICE_URL` o `INTERNAL_API_KEY` en `.env`, o si el document-service devuelve error / el PDF no se descarga, `_materialize_quote` devuelve `None` y el bot responde `QUOTE_PENDING_MSG` (texto genérico) — no se manda ningún PDF y la única pista es un `logger.warning`. Si "el PDF no llega", revisa esos logs antes de tocar código.
- **Cotización sin ítems determinables → preguntar, nunca prometer**: si la extracción devuelve `items: []` (p. ej. un pedido genérico como "cotillón para pasado mañana" porque el precio del cotillón varía según tamaño e invitados), el bot responde con `QUOTE_CLARIFY_MSG` pidiendo productos/cantidades, nombre y fecha, y marca la conversación en `_pending_quote_info`. La siguiente respuesta del cliente se extrae con `force_quote=True` (intent obligado a "quote") aunque no traiga keywords. El prompt de extracción recibe la fecha de hoy (`{today}`) para calcular fechas relativas como "pasado mañana" y las pone en `event_date`; como la plantilla del document-service **no tiene campo de fecha**, la fecha/nº de personas se inyectan en la descripción del primer ítem y el título, y los ítems con `unit_price: 0.0` se marcan como "precios a confirmar según tamaño" en el caption.
- **Reenvío determinista + cierre social**: si la conversación ya recibió un PDF (`_last_quote_pdf`), mensajes como "envíame el archivo pdf", "pásame el pdf" o "mándamela" reenvían **ese** PDF almacenado sin llamar al LLM (`_is_pure_resend`; el gate de cotización incluye `pdf`/`archivo`/`adjunto`/`reenvi`). Mensajes con datos de cotización nueva (personas, precios, "para...", etc.) NO se consideran reenvío puro y van a la extracción. Los "gracias"/cierres puros responden con `SOCIAL_CLOSE_MSG` (una línea, sin LLM) y limpian `_pending_quote_info`.
- **Cierre automático por inactividad (una sola etapa)**: tras la última respuesta del bot, si el cliente no escribe en `IDLE_CLOSE_SECONDS` (env, default 600 = 10 min), un `_idle_monitor` (arrancado vía `lifespan`, revisa cada `IDLE_CHECK_INTERVAL`) envía `CLOSE_FINAL_MSG` y llamada `_close_conversation` limpia memoria, cotizaciones y PDFs. `_track_idle`/`_clear_idle` (`main.py:180-238`) registran el contador por `conversation_id`; el monitor decide el canal por el prefijo del `conversation_id` (`whatsapp:`/`messenger:`/`telegram:` — `main.py:247-256`) y un canal desconocido solo loguea un warning sin enviar nada.
- **State en memoria con excepción para el dedup**: memoria de conversación (`_conv_histories`, tope `CONV_HISTORY_LIMIT = 8` mensajes), cotizaciones pendientes (`_pending_quote_info`) y último PDF (`_last_quote_pdf`) viven en memoria y se borran al reiniciar. El dedup de WhatsApp (`_recent_wamids`, `main.py:244-280`) **y el de Messenger** (`_recent_mids`, `main.py:302-341`) **sí son persistentes**: se guardan en `logs/recent_wamids.json` / `logs/recent_mids.json` (gitignored) en cada update y se recargan al importar, con TTL de 24 h y tope de 1000 entradas (`WAMID_TTL_SECONDS`/`MID_TTL_SECONDS`, `_RECENT_*_LIMIT`). Así un reinicio no vuelve a responder un retry de Meta ya atendido. `/chat` también recibe estos wamids con `conversation_id` vía `_handle_*_message`.
- **Entregas viejas de Meta se descartan**: los `parse_messages` de WhatsApp, Messenger e Instagram ignoran mensajes cuyo `timestamp` supere `STALE_MESSAGE_SECONDS` (900 s ≈ 15 min; los tres adapters) — son retries de Meta de mensajes ya respondidos; sin esto el bot "habla solo" tras una caída/lentitud prolongada del proceso. Messenger envía el timestamp en **milisegundos** y lo divide a 1000 antes de comparar.

## Run a single test / verification

No integration test suite. Minimal unittests with a temp SQLite (stdlib, no extra deps):

```bash
.venv/bin/python -m unittest tests/test_metrics.py tests/test_whatsapp_metrics.py tests/test_costs.py -v
```

Informe de costes/rentabilidad de un período (siembra tarifas por defecto salvo `--no-seed`):

```bash
.venv/bin/python cost_report.py --period 2026-10
```

End-to-end manual verification:

```bash
# Start server (default port 8001; 8000 is the document-service)
uvicorn main:app --port 8001

# Index sample data
curl -X POST http://127.0.0.1:8001/documents/upload -F "file=@data/business_data.txt"

# Chat (registra un evento usage_events chat_reply en metrics.sqlite3)
curl -X POST http://127.0.0.1:8001/chat -H "Content-Type: application/json" -d '{"message": "test"}'

# Health check (shows configured providers)
curl http://127.0.0.1:8001/health
```

To inspect recorded events:

```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('metrics.sqlite3'); print(c.execute('select event_type, provider, model, total_tokens, tenant_id from usage_events order by id').fetchall())"
```

## Language

Code comments, README, and user-facing strings are in Spanish. Maintain this convention.

## Telegram bot

`run_bot.sh` starts uvicorn + a Cloudflare quick tunnel and registers the webhook. Subcommands: `start` (default), `stop`, `webhook` (re-register). Requires `TELEGRAM_BOT_TOKEN` in `.env` (it auto-generates `TELEGRAM_WEBHOOK_SECRET` if missing) and `cloudflared` in PATH or as `./cloudflared`. The free `trycloudflare.com` URL changes each restart, so `webhook` must be re-registered after a tunnel restart.

- `/webhooks/telegram` procesa el mensaje en un `BackgroundTasks` (responde 200 rápido) y deduplica por `update_id` (`_seen_update_ids`, en memoria, tope 1000) para no responder dos veces si Telegram reintenta. Si `/chat` falla (503), el error se registra y el webhook igual responde 200 — Telegram no reintenta un update ya confirmado.

## WhatsApp bot (Meta Cloud API)

- `run_whatsapp.sh` starts uvicorn on port 8002 + a tunnel and **prints** the URL: Meta does NOT allow API webhook registration, the URL + verify token must be pasted manually in the dev dashboard (App → WhatsApp → Configuration → Webhook, and subscribe to `messages`). After a tunnel restart the URL must be re-pasted. Also needs `cloudflared` in PATH or as `./cloudflared`.
- **Each run script only kills its own processes**: they write channel-specific pids (`logs/telegram_uvicorn.pid` + `telegram_cloudflared.pid` vs `logs/whatsapp_uvicorn.pid` + `whatsapp_cloudflared.pid` vs `logs/messenger_uvicorn.pid` + `messenger_cloudflared.pid`, see `run_bot.sh:144,160` / `run_whatsapp.sh:94,110` / `run_messenger.sh:107,123`). `stop` from one no longer kills the others. Ports are fixed: telegram bot on **8001** (`run_bot.sh:23`), whatsapp bot on **8002**, messenger bot on **8004** (`run_messenger.sh:26`), document-service on **8000** — never run the bot on 8000, that's the document-service.
- All three run scripts skip launching uvicorn if `http://127.0.0.1:$PORT/health` already responds **with `"providers_configured"`** (`run_bot.sh:139` / `run_whatsapp.sh:89` / `run_messenger.sh:102`) — any other service answering on that port is not reused as the bot.
- **Three unrelated secrets**: `WHATSAPP_VERIFY_TOKEN` (handshake only), `WHATSAPP_APP_SECRET` (HMAC signature), `WHATSAPP_ACCESS_TOKEN` (send API).
- The `X-Hub-Signature-256` is HMAC-SHA256 over the **raw request body** using the app secret — `main.py` reads `await request.body()` and never re-serializes the parsed JSON to verify (see `verify_signature` in `whatsapp_adapter.py`).
- GET `/webhooks/whatsapp` must echo `hub.challenge` as **plain text** (no JSON) or the dashboard verification fails.
- **24h service window**: free-form replies only work within 24h of the customer's last message; outside that window only approved templates are accepted (`send_message` failures are just logged).
- Meta retries failed deliveries → `main.py` dedups by `wamid` (persistido en `logs/recent_wamids.json`, ver gotchas) y procesa cada mensaje en un FastAPI `BackgroundTasks` para que el webhook responda 200 rápido.
- Incoming text only: non-text types (media, interactive, buttons) are logged and ignored in `parse_messages`.
- Only `type: text` messages from `entry[].changes[].value.messages[]` are handled; `conversation_id` is `whatsapp:<wa_id>`.

## Messenger bot (Meta Graph API)

- `run_messenger.sh` starts uvicorn on port 8004 + a tunnel and **prints** the URL: igual que WhatsApp, Meta no permite registrar el webhook por API — la URL + verify token se pegan a mano en el panel (App → Messenger → Configuration → Webhook, suscribirse a `messages`). Tras reiniciar el túnel hay que re-pegarla. Requiere `cloudflared` en PATH o como `./cloudflared`.
- **Secrets**: `MESSENGER_VERIFY_TOKEN` (handshake, se responde el challenge en **texto plano**), `MESSENGER_APP_SECRET` (firma HMAC), `MESSENGER_ACCESS_TOKEN` (send API), `MESSENGER_PAGE_ID` (detectar ecos).
- La firma `X-Hub-Signature-256` se verifica sobre el **body crudo** (`main.py:1108-1113`), igual que WhatsApp.
- **Echoes descartados**: los mensajes que envió la propia Página llegan como eco — `parse_messages` los ignora por `message.is_echo`, por `sender.id == MESSENGER_PAGE_ID` y por el array `message_echoes` (`messenger_adapter.py:80-117`; Meta no garantiza que vengan los tres, por eso se comprueban todos). Sin esto el bot se responde a sí mismo en bucle.
- Dedup por `mid` persistido en `logs/recent_mids.json` (ver gotchas); cada mensaje procesa en `BackgroundTasks` (`_handle_messenger_message`).
- **Solo texto**: adjuntos/stickers/quick replies se loguean y se ignoran. El texto de respuesta se limpia de Markdown (`_format_for_messenger`) y se trocea en mensajes de ≤ 2000 chars (`_split_text`, corta en párrafos → líneas → espacios).
- `conversation_id` es `messenger:<psid>`; el **psid no es un teléfono**, así que el flujo de cotización pasa `fallback_phone=""` (`main.py:1135`).
- `parse_messages` descarta timestamps con más de 900 s (el timestamp llega en **milisegundos**).

## Instagram (solo recepción)

- `GET/POST /webhooks/instagram` existe y valida el handshake con `INSTAGRAM_VERIFY_TOKEN`, pero el POST **solo parsea y loguea** los mensajes (`main.py:405-413`) — `instagram_adapter.py` no tiene función de envío y el flujo de cotización no está conectado. No respondas por este canal hasta que se implemente el envío.
