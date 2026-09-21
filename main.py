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

import asyncio
import hmac
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel

import rag
import telegram_adapter
import whatsapp_adapter
import document_service

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatbot")

app = FastAPI(title="SMB Chatbot Assistant")

# Configuración de seguridad del webhook (no hardcodeada, viene de .env).
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET") or ""
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# WhatsApp Business Cloud API (Meta). El verify_token es el que se pega en el
# panel de Meta (distinto del App Secret) y el App Secret valida la firma de
# los updates. Vacíos = canal deshabilitado / validación opcional.
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN") or ""
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET") or ""


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


class QuoteRequest(BaseModel):
    """Payload para generar una cotización PDF en el document-service."""
    request_id: str
    tenant_id: str
    customer: dict
    items: list[dict]
    currency: str = "DOP"


class QuoteResponse(BaseModel):
    document_id: str
    quote_number: str
    pdf_url: str
    size_bytes: int
    status: str


def build_system_prompt(context: str) -> str:
    if not context:
        return BASE_SYSTEM_PROMPT
    return BASE_SYSTEM_PROMPT + RAG_INSTRUCTIONS.format(context=context)


# Memoria de conversación en memoria, indexada por conversation_id. Se limita
# a los últimos N mensajes para no crecer sin control (un reinicio del proceso
# equivale a una sesión nueva).
CONV_HISTORY_LIMIT = 8
_conv_histories: dict[str, list[dict]] = {}

# --- Cierre automático por inactividad (Flujo de Cierre, 2 etapas) ----------
# Tras el último mensaje del usuario:
#   1er aviso (cortesía) a los IDLE_NAG_SECONDS  (3 min) — llama la atención
#   cierre  automático     a los IDLE_CLOSE_SECONDS (5 min = 3+2) — despedida +
#   limpieza del contexto para la próxima sesión.
IDLE_NAG_SECONDS = 180   # 3:00
IDLE_CLOSE_SECONDS = 300 # 5:00
IDLE_CHECK_INTERVAL = 20 # cada cuánto revisa el monitor (s)

# conversation_id -> timestamp (epoch) del último mensaje del usuario.
_idle_last_activity: dict[str, float] = {}
# conversation_id -> wamid/chat_id del canal para responder (wa_id o chat_id).
_idle_target_hint: dict[str, str] = {}

CLOSE_COURTESY_MSG = (
    "Hola, ¿sigues ahí? Cuéntame si necesitas algo más o si podemos dar por "
    "terminada nuestra sesión."
)
CLOSE_FINAL_MSG = (
    "Como no he tenido respuesta, daré por cerrada esta conversación para "
    "mantener tus datos seguros. Si me necesitas de nuevo, solo escribe un "
    "mensaje. ¡Que tengas un buen día!"
)

# Deduplicación de updates de WhatsApp: Meta reintenta los envíos fallidos, así
# que un mismo wamid puede llegar más de una vez.
_RECENT_WAMIDS_LIMIT = 200
_recent_wamids: set[str] = set()


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


@app.post("/documents/quote", response_model=QuoteResponse)
async def create_quote(request: QuoteRequest) -> QuoteResponse:
    """Genera una cotización PDF delegando en el microservicio document-service."""
    result = await document_service.create_quote(
        request_id=request.request_id,
        tenant_id=request.tenant_id,
        customer=request.customer,
        items=request.items,
        currency=request.currency,
    )
    if result is None:
        raise HTTPException(
            status_code=502,
            detail="El document-service no pudo generar la cotización",
        )
    return QuoteResponse(
        document_id=result.document_id,
        quote_number=result.quote_number,
        pdf_url=result.pdf_url,
        size_bytes=result.size_bytes,
        status=result.status,
    )


@app.get("/health")
async def health() -> dict:
    configured = [p.name for p in PROVIDERS if p.api_key]
    return {
        "status": "ok",
        "providers_configured": configured,
        "documents_indexed": len(rag.list_sources()),
        "channels": {
            "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
            "whatsapp": bool(os.getenv("WHATSAPP_ACCESS_TOKEN")) and bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID")),
        },
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


@app.get("/webhooks/whatsapp")
async def whatsapp_webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Handshake de verificación de Meta. Debe responder con el challenge
    CRUDO (texto plano, no JSON) si el verify_token coincide."""
    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="WHATSAPP_VERIFY_TOKEN no configurado")
    if hub_mode == "subscribe" and hmac.compare_digest(
        hub_verify_token or "", WHATSAPP_VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verify token inválido")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Recibe los events de WhatsApp. Lee el body crudo para validar la firma
    y procesa la conversación en background para responder 200 rápido (Meta
    reintenta si tarda o falla)."""
    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="WHATSAPP_VERIFY_TOKEN no configurado")

    raw_body = await request.body()

    if WHATSAPP_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256")
        if not whatsapp_adapter.verify_signature(raw_body, signature, WHATSAPP_APP_SECRET):
            raise HTTPException(status_code=401, detail="Firma de webhook inválida")
    try:
        update = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body inválido")

    for wamid, wa_id, message in whatsapp_adapter.parse_messages(update):
        if wamid in _recent_wamids:
            logger.info("Update de WhatsApp duplicado ignorado (wamid %s)", wamid)
            continue
        _recent_wamids.add(wamid)
        if len(_recent_wamids) > _RECENT_WAMIDS_LIMIT:
            _recent_wamids.clear()
        background_tasks.add_task(_handle_whatsapp_message, wamid, wa_id, message)

    return {"status": "ok"}


async def _handle_whatsapp_message(wamid: str, wa_id: str, message: str) -> None:
    """Procesa un mensaje de WhatsApp: llama a /chat y devuelve la respuesta."""
    request = ChatRequest(
        message=message,
        conversation_id=whatsapp_adapter.build_conversation_id(wa_id),
    )
    try:
        response = await chat(request)
    except HTTPException as exc:
        logger.error("Error al responder por WhatsApp (wamid %s): %s", wamid, exc.detail)
        return
    await whatsapp_adapter.send_message(wa_id, response.reply)
