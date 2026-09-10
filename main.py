"""
Chatbot backend para pymes con:
  - Fallback automático entre APIs gratuitas (Groq -> OpenRouter -> Gemini)
  - RAG: consulta documentos del negocio (.txt/.pdf) para no inventar respuestas

Requisitos:
    pip install -r requirements.txt

Variables de entorno (ver .env.example):
    GROQ_API_KEY
    OPENROUTER_API_KEY
    GEMINI_API_KEY
"""

import logging
import os
import tempfile
from dataclasses import dataclass

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel

import rag
import telegram_adapter

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatbot")

app = FastAPI(title="SMB Chatbot Assistant")

# Configuración de seguridad del webhook (no hardcodeada, viene de .env).
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET") or ""
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def verify_webhook_secret(x_telegram_bot_api_secret_token: str | None = None) -> None:
    """Valida el origen del webhook. Si hay un secret configurado, exige que la
    cabecera coincida; si no, responde 401 (evita usar /webhooks/telegram como
    proxy hacia /chat)."""
    if not TELEGRAM_WEBHOOK_SECRET:
        return
    if x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Secret de webhook inválido")


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str | None
    model: str


# Orden = prioridad de intento. Ajusta modelos/orden según tu caso de uso.
PROVIDERS: list[Provider] = [
    Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key=os.getenv("GROQ_API_KEY"),
        model="openai/gpt-oss-120b",
    ),
    Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=os.getenv("OPENROUTER_API_KEY"),
        model="meta-llama/llama-3.3-70b-instruct:free",
    ),
    Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=os.getenv("GEMINI_API_KEY"),
        model="gemini-2.5-flash",
    ),
]

BASE_SYSTEM_PROMPT = (
    "Eres un asistente virtual para un pequeño o mediano negocio. "
    "Responde de forma breve, clara y amable. "
    "Si ya ofreciste los datos de contacto del negocio (WhatsApp, teléfono o "
    "correo) en esta conversación, no vuelvas a mencionarlos: repítelos solo si "
    "el usuario los pide de nuevo o si se está despidiendo."
)

RAG_INSTRUCTIONS = (
    "\n\nUsa ÚNICAMENTE la siguiente información del negocio para responder. "
    "Si la respuesta no está en esta información, di claramente que no cuentas "
    "con ese dato y sugiere que la persona contacte al negocio directamente. "
    "No inventes precios, horarios, políticas ni datos que no aparezcan aquí.\n\n"
    "--- INFORMACIÓN DEL NEGOCIO ---\n{context}\n--- FIN DE LA INFORMACIÓN ---"
)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    provider_used: str
    model_used: str
    used_context: bool


def build_system_prompt(context: str) -> str:
    if not context:
        return BASE_SYSTEM_PROMPT
    return BASE_SYSTEM_PROMPT + RAG_INSTRUCTIONS.format(context=context)


# Memoria de conversación en memoria, indexada por conversation_id. Se limita
# a los últimos N mensajes para no crecer sin control (un reinicio del proceso
# equivale a una sesión nueva).
CONV_HISTORY_LIMIT = 8
_conv_histories: dict[str, list[dict]] = {}


def _append_history(conversation_id: str | None, messages: list[dict]) -> None:
    if not conversation_id:
        return
    hist = _conv_histories.setdefault(conversation_id, [])
    hist.extend(messages)
    _conv_histories[conversation_id] = hist[-CONV_HISTORY_LIMIT:]


async def _try_provider(provider: Provider, messages: list[dict]) -> str:
    if not provider.api_key:
        raise ValueError(f"{provider.name}: falta API key")

    client = AsyncOpenAI(base_url=provider.base_url, api_key=provider.api_key)
    completion = await client.chat.completions.create(
        model=provider.model,
        messages=messages,
        timeout=15,
    )
    return completion.choices[0].message.content or ""


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    context = rag.retrieve_context(request.message)
    system_prompt = build_system_prompt(context)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(_conv_histories.get(request.conversation_id or "", []))
    messages.append({"role": "user", "content": request.message})

    errors: list[str] = []

    for provider in PROVIDERS:
        try:
            reply = await _try_provider(provider, messages)
            logger.info("Respondido por %s (%s)", provider.name, provider.model)
            _append_history(request.conversation_id, [
                {"role": "user", "content": request.message},
                {"role": "assistant", "content": reply},
            ])
            return ChatResponse(
                reply=reply,
                provider_used=provider.name,
                model_used=provider.model,
                used_context=bool(context),
            )
        except (RateLimitError, APITimeoutError, APIError, ValueError) as exc:
            logger.warning("Fallo en %s: %s", provider.name, exc)
            errors.append(f"{provider.name}: {exc}")
            continue

    raise HTTPException(
        status_code=503,
        detail=f"Todos los providers fallaron: {'; '.join(errors)}",
    )


@app.post("/documents/upload")
async def upload_document(file: UploadFile = File(...)) -> dict:
    suffix = "." + file.filename.split(".")[-1].lower()
    if suffix not in (".txt", ".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .txt o .pdf")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        chunks_created = rag.ingest_document(tmp_path, source_name=file.filename)
    finally:
        os.unlink(tmp_path)

    if chunks_created == 0:
        raise HTTPException(status_code=422, detail="No se pudo extraer texto del documento")

    return {"filename": file.filename, "chunks_indexed": chunks_created}


@app.get("/documents")
async def list_documents() -> dict:
    return {"sources": rag.list_sources()}


@app.delete("/documents/{source_name}")
async def delete_document(source_name: str) -> dict:
    deleted = rag.delete_source(source_name)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return {"deleted_chunks": deleted}


@app.get("/health")
async def health() -> dict:
    configured = [p.name for p in PROVIDERS if p.api_key]
    return {
        "status": "ok",
        "providers_configured": configured,
        "documents_indexed": len(rag.list_sources()),
    }


@app.post("/webhooks/telegram")
async def telegram_webhook(
    update: dict,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    verify_webhook_secret(x_telegram_bot_api_secret_token)

    chat_id = telegram_adapter.get_chat_id(update)
    message = telegram_adapter.parse_message(update)

    if chat_id is None or message is None:
        logger.info("Update de Telegram ignorado (sin chat_id o sin texto)")
        return {"status": "ok", "ignored": True}

    request = ChatRequest(
        message=message,
        conversation_id=telegram_adapter.build_conversation_id(chat_id),
    )
    response = await chat(request)

    await telegram_adapter.send_message(chat_id, response.reply)
    return {"status": "ok"}
