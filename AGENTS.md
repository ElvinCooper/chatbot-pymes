# AGENTS.md

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add at least one API key
uvicorn main:app --reload
```

## Architecture

Flat Python project, no packages. All source files are in the root:

| File | Role |
|---|---|
| `main.py` | FastAPI app: `/chat`, `/documents/*`, `/health`, `/webhooks/telegram` |
| `rag.py` | ChromaDB ingestion (chunking, indexing) and retrieval |
| `embeddings.py` | Local multilingual E5-small via ONNX (no API key needed) |
| `telegram_adapter.py` | Telegram Bot API helpers (parse updates, send messages) |

## Key gotchas

- **Embedding model downloads on first use** (~226 MB to `~/.cache/huggingface`). Needs internet once, then runs fully offline.
- **ChromaDB vector store** is in `chroma_db/` (gitignored, regenerable). To reset: `rm -rf chroma_db/` then re-upload documents.
- **E5 embedding prefixes** matter: documents are prefixed `passage: `, queries are prefixed `query: `. Mixing them up breaks retrieval quality. See `embeddings.py:140-156`.
- **LLM provider fallback**: Groq → OpenRouter → Gemini, automatic. At least one `*_API_KEY` in `.env` is required or `/chat` returns 503.
- **Collection auto-recreation**: If you change the embedding model or distance metric, ChromaDB detects the mismatch and recreates the collection. Existing documents must be re-indexed.

## Run a single test / verification

No test suite exists. To verify the system works end-to-end:

```bash
# Start server
uvicorn main:app --reload

# Index sample data
curl -X POST http://127.0.0.1:8000/documents/upload -F "file=@data/business_data.txt"

# Chat
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" -d '{"message": "test"}'

# Health check (shows configured providers)
curl http://127.0.0.1:8000/health
```

## Language

Code comments, README, and user-facing strings are in Spanish. Maintain this convention.

## Telegram bot

`run_bot.sh` starts uvicorn + a Cloudflare quick tunnel and registers the webhook. Subcommands: `start` (default), `stop`, `webhook` (re-register). Requires `TELEGRAM_BOT_TOKEN` in `.env` (it auto-generates `TELEGRAM_WEBHOOK_SECRET` if missing). The free `trycloudflare.com` URL changes each restart, so `webhook` must be re-registered after a tunnel restart.
