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
| `main.py` | FastAPI app: `/chat`, `/documents/*`, `/health`, `/webhooks/telegram`, `/webhooks/whatsapp`. `/documents/quote` delega en `document_service.py`; los webhooks detectan cotizaciones y envían el PDF como archivo adjunto (`send_document`) |
| `rag.py` | ChromaDB ingestion (chunking, indexing) and retrieval |
| `embeddings.py` | Local multilingual E5-small via ONNX (no API key needed) |
| `telegram_adapter.py` | Telegram Bot API helpers (parse updates, send messages, send documents) |
| `whatsapp_adapter.py` | WhatsApp Cloud API helpers (signature check, parse/send, send documents) |
| `document_service.py` | Client for the external document-service (`POST /quotes` → PDF, fetch_quote_pdf) |
| `email_service.py` | Client for the external email-service (`POST /api/v1/emails/send` → email_id). **Todavía no lo llama ningún flujo**: queda listo y verificado, sin uso |

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
- **Collection auto-recreation**: `rag.py` checks collection metadata at **import time** (`rag.py:36-67`). If the embedding model or distance metric changed, ChromaDB detects the mismatch and recreates the collection — existing documents must be re-indexed.
- **Side effects at import**: `main.py` calls `load_dotenv()` and builds `PROVIDERS` from env; `rag.py` opens ChromaDB, loads the ONNX model, and runs the recreate check on `import`. Changes to `.env`, chunk sizes, or collection metadata require a full process restart (no hot reload).
- **Cotizaciones = archivo adjunto, nunca texto**: si un mensaje pide una cotización, los webhooks extraen la estructura con el LLM (`_extract_quote_draft`), generan el PDF en el document-service, lo descargan (`fetch_quote_pdf`) y lo envían con `send_document` (WhatsApp sube media a la Graph API → mensaje tipo `document`; Telegram usa `sendDocument`). El system prompt prohíbe al LLM emitir PDFs en base64. Si la extracción falla, el mensaje cae al `/chat` normal.
- **Fallos de cotización silenciosos**: sin `DOCUMENT_SERVICE_URL` o `INTERNAL_API_KEY` en `.env`, o si el document-service devuelve error / el PDF no se descarga, `_materialize_quote` devuelve `None` y el bot responde `QUOTE_PENDING_MSG` (texto genérico) — no se manda ningún PDF y la única pista es un `logger.warning`. Si "el PDF no llega", revisa esos logs antes de tocar código.
- **Cotización sin ítems determinables → preguntar, nunca prometer**: si la extracción devuelve `items: []` (p. ej. un pedido genérico como "cotillón para pasado mañana" porque el precio del cotillón varía según tamaño e invitados), el bot responde con `QUOTE_CLARIFY_MSG` pidiendo productos/cantidades, nombre y fecha, y marca la conversación en `_pending_quote_info`. La siguiente respuesta del cliente se extrae con `force_quote=True` (intent obligado a "quote") aunque no traiga keywords. El prompt de extracción recibe la fecha de hoy (`{today}`) para calcular fechas relativas como "pasado mañana" y las pone en `event_date`; como la plantilla del document-service **no tiene campo de fecha**, la fecha/nº de personas se inyectan en la descripción del primer ítem y el título, y los ítems con `unit_price: 0.0` se marcan como "precios a confirmar según tamaño" en el caption.
- **Reenvío determinista + cierre social**: si la conversación ya recibió un PDF (`_last_quote_pdf`), mensajes como "envíame el archivo pdf", "pásame el pdf" o "mándamela" reenvían **ese** PDF almacenado sin llamar al LLM (`_is_pure_resend`; el gate de cotización incluye `pdf`/`archivo`/`adjunto`/`reenvi`). Mensajes con datos de cotización nueva (personas, precios, "para...", etc.) NO se consideran reenvío puro y van a la extracción. Los "gracias"/cierres puros responden con `SOCIAL_CLOSE_MSG` (una línea, sin LLM) y limpian `_pending_quote_info`.
- **Cierre automático por inactividad (una sola etapa)**: tras la última respuesta del bot, si el cliente no escribe en `IDLE_CLOSE_SECONDS` (env, default 600 = 10 min), un `_idle_monitor` (arrancado vía `lifespan`, revisa cada `IDLE_CHECK_INTERVAL`) envía `CLOSE_FINAL_MSG` y llamada `_close_conversation` limpia memoria, cotizaciones y PDFs. `_track_idle`/`_clear_idle` (`main.py:180-238`) registran el contador por `conversation_id`.
- **State en memoria con excepción para el dedup**: memoria de conversación (`_conv_histories`), cotizaciones pendientes (`_pending_quote_info`) y último PDF (`_last_quote_pdf`) viven en memoria y se borran al reiniciar. El dedup de wamids de WhatsApp (`_recent_wamids`, `main.py:244-280`) **sí es persistente**: se guarda en `logs/recent_wamids.json` (gitignored) en cada update y se recarga al importar, con TTL de 24 h y tope de 1000 entradas (`WAMID_TTL_SECONDS`, `_RECENT_WAMIDS_LIMIT`). Así un reinicio no vuelve a responder un retry de Meta ya atendido. `/chat` también recibe estos wamids con `conversation_id` vía `_handle_*_message`.
- **Entregas viejas de Meta se descartan**: `parse_messages` ignora mensajes cuyo `timestamp` supere `STALE_MESSAGE_SECONDS` (900 s ≈ 15 min, `whatsapp_adapter.py:41`) — son retries de Meta de mensajes ya respondidos; sin esto el bot "habla solo" tras una caída/lentitud prolongada del proceso.

## Run a single test / verification

No test suite exists. To verify the system works end-to-end:

```bash
# Start server (default port 8001; 8000 is the document-service)
uvicorn main:app --port 8001

# Index sample data
curl -X POST http://127.0.0.1:8001/documents/upload -F "file=@data/business_data.txt"

# Chat
curl -X POST http://127.0.0.1:8001/chat -H "Content-Type: application/json" -d '{"message": "test"}'

# Health check (shows configured providers)
curl http://127.0.0.1:8001/health
```

## Language

Code comments, README, and user-facing strings are in Spanish. Maintain this convention.

## Telegram bot

`run_bot.sh` starts uvicorn + a Cloudflare quick tunnel and registers the webhook. Subcommands: `start` (default), `stop`, `webhook` (re-register). Requires `TELEGRAM_BOT_TOKEN` in `.env` (it auto-generates `TELEGRAM_WEBHOOK_SECRET` if missing) and `cloudflared` in PATH or as `./cloudflared`. The free `trycloudflare.com` URL changes each restart, so `webhook` must be re-registered after a tunnel restart.

- `/webhooks/telegram` procesa el mensaje en un `BackgroundTasks` (responde 200 rápido) y deduplica por `update_id` (`_seen_update_ids`, en memoria, tope 1000) para no responder dos veces si Telegram reintenta. Si `/chat` falla (503), el error se registra y el webhook igual responde 200 — Telegram no reintenta un update ya confirmado.

## WhatsApp bot (Meta Cloud API)

- `run_whatsapp.sh` starts uvicorn on port 8002 + a tunnel and **prints** the URL: Meta does NOT allow API webhook registration, the URL + verify token must be pasted manually in the dev dashboard (App → WhatsApp → Configuration → Webhook, and subscribe to `messages`). After a tunnel restart the URL must be re-pasted. Also needs `cloudflared` in PATH or as `./cloudflared`.
- **Each run script only kills its own processes**: they write channel-specific pids (`logs/telegram_uvicorn.pid` + `telegram_cloudflared.pid` vs `logs/whatsapp_uvicorn.pid` + `whatsapp_cloudflared.pid`, see `run_bot.sh:144,160` / `run_whatsapp.sh:94,110`). `stop` from one no longer kills the other. Ports are fixed: telegram bot on **8001** (`run_bot.sh:23`), whatsapp bot on **8002**, document-service on **8000** — never run the bot on 8000, that's the document-service.
- Both scripts skip launching uvicorn if `http://127.0.0.1:$PORT/health` already responds **with `"providers_configured"`** (`run_bot.sh:139` / `run_whatsapp.sh:89`) — any other service answering on that port is not reused as the bot.
- **Three unrelated secrets**: `WHATSAPP_VERIFY_TOKEN` (handshake only), `WHATSAPP_APP_SECRET` (HMAC signature), `WHATSAPP_ACCESS_TOKEN` (send API).
- The `X-Hub-Signature-256` is HMAC-SHA256 over the **raw request body** using the app secret — `main.py` reads `await request.body()` and never re-serializes the parsed JSON to verify (see `verify_signature` in `whatsapp_adapter.py`).
- GET `/webhooks/whatsapp` must echo `hub.challenge` as **plain text** (no JSON) or the dashboard verification fails.
- **24h service window**: free-form replies only work within 24h of the customer's last message; outside that window only approved templates are accepted (`send_message` failures are just logged).
- Meta retries failed deliveries → `main.py` dedups by `wamid` (persistido en `logs/recent_wamids.json`, ver gotchas) y procesa cada mensaje en un FastAPI `BackgroundTasks` para que el webhook responda 200 rápido.
- Incoming text only: non-text types (media, interactive, buttons) are logged and ignored in `parse_messages`.
- Only `type: text` messages from `entry[].changes[].value.messages[]` are handled; `conversation_id` is `whatsapp:<wa_id>`.
