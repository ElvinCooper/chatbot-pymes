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

## Key gotchas

- **Embedding model downloads on first use** (~226 MB to `~/.cache/huggingface`). Needs internet once, then runs fully offline.
- **ChromaDB vector store** is in `chroma_db/` (gitignored, regenerable). To reset: `rm -rf chroma_db/` then re-upload documents.
- **E5 embedding prefixes** matter: documents are prefixed `passage: `, queries are prefixed `query: ` (see `QUERY_PREFIX`/`PASSAGE_PREFIX`, `embeddings.py:31-32`). Mixing them up breaks retrieval quality.
- **LLM provider fallback**: Groq → OpenRouter → Gemini, automatic. Models are hardcoded in `PROVIDERS` (`main.py:74-88`). At least one `*_API_KEY` in `.env` is required or `/chat` returns 503.
- **Collection auto-recreation**: `rag.py` checks collection metadata at **import time** (`rag.py:36-67`). If the embedding model or distance metric changed, ChromaDB detects the mismatch and recreates the collection — existing documents must be re-indexed.
- **Side effects at import**: `main.py` calls `load_dotenv()` and builds `PROVIDERS` from env; `rag.py` opens ChromaDB, loads the ONNX model, and runs the recreate check on `import`. Changes to `.env`, chunk sizes, or collection metadata require a full process restart (no hot reload).
- **Cotizaciones = archivo adjunto, nunca texto**: si un mensaje pide una cotización, los webhooks extraen la estructura con el LLM (`_extract_quote_draft`), generan el PDF en el document-service, lo descargan (`fetch_quote_pdf`) y lo envían con `send_document` (WhatsApp sube media a la Graph API → mensaje tipo `document`; Telegram usa `sendDocument`). El system prompt prohíbe al LLM emitir PDFs en base64. Si la extracción falla, el mensaje cae al `/chat` normal.
- **Conversation memory (`_conv_histories` + `_idle_*`) and WhatsApp wamid dedup (`_recent_wamids`) are in-memory** (`main.py:166-184`): a restart wipes all sessions and lets a Meta retry of the same `wamid` through again.

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

## WhatsApp bot (Meta Cloud API)

- `run_whatsapp.sh` starts uvicorn on port 8002 + a tunnel and **prints** the URL: Meta does NOT allow API webhook registration, the URL + verify token must be pasted manually in the dev dashboard (App → WhatsApp → Configuration → Webhook, and subscribe to `messages`). After a tunnel restart the URL must be re-pasted. Also needs `cloudflared` in PATH or as `./cloudflared`.
- **Each run script only kills its own processes**: they write channel-specific pids (`logs/telegram_uvicorn.pid` + `telegram_cloudflared.pid` vs `logs/whatsapp_uvicorn.pid` + `whatsapp_cloudflared.pid`, see `run_bot.sh:144,160` / `run_whatsapp.sh:94,110`). `stop` from one no longer kills the other. Ports are fixed: telegram bot on **8001** (`run_bot.sh:23`), whatsapp bot on **8002**, document-service on **8000** — never run the bot on 8000, that's the document-service.
- Both scripts skip launching uvicorn if `http://127.0.0.1:$PORT/health` already responds **with `"providers_configured"`** (`run_bot.sh:139` / `run_whatsapp.sh:89`) — any other service answering on that port is not reused as the bot.
- **Three unrelated secrets**: `WHATSAPP_VERIFY_TOKEN` (handshake only), `WHATSAPP_APP_SECRET` (HMAC signature), `WHATSAPP_ACCESS_TOKEN` (send API).
- The `X-Hub-Signature-256` is HMAC-SHA256 over the **raw request body** using the app secret — `main.py` reads `await request.body()` and never re-serializes the parsed JSON to verify (see `verify_signature` in `whatsapp_adapter.py`).
- GET `/webhooks/whatsapp` must echo `hub.challenge` as **plain text** (no JSON) or the dashboard verification fails.
- **24h service window**: free-form replies only work within 24h of the customer's last message; outside that window only approved templates are accepted (`send_message` failures are just logged).
- Meta retries failed deliveries → `main.py` dedups by `wamid` and processes each message in a FastAPI `BackgroundTasks` so the webhook returns 200 fast.
- Incoming text only: non-text types (media, interactive, buttons) are logged and ignored in `parse_messages`.
- Only `type: text` messages from `entry[].changes[].value.messages[]` are handled; `conversation_id` is `whatsapp:<wa_id>`.
