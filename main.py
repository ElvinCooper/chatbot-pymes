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
import datetime
import hmac
import json
import logging
import os
import re
import secrets
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
        model="meta-llama/llama-3.3-70b-instruct",
    ),
    Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=os.getenv("GEMINI_API_KEY"),
        model="gemini-3.6-flash",
    ),
]

BASE_SYSTEM_PROMPT = (
    "Eres un asistente virtual para un pequeño o mediano negocio. "
    "Responde de forma breve, clara y amable. "
    "NUNCA muestres PDFs en base64 ni instrucciones para decodificarlos: las "
    "cotizaciones se envían como archivo PDF por separado, nunca por texto. "
    "Si el cliente pide una cotización y EN ESTA CONVERSACIÓN ya se le envió "
    "como PDF adjunto, NO prometas prepararla ni enviarla de nuevo: confirma "
    "simplemente que ya se le envió y, si insiste, dile que se la reenviará "
    "adjunta. Si la cotización todavía no está enviada, dile solo que la "
    "cotización se le enviará en PDF por este chat, sin asegurar que es "
    "inmediata. "
    "Si el usuario solo agradece, saluda o se despide, responde en una sola "
    "línea, amable, y cierra el tema sin extender la conversación. "
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


# --- Cotizaciones en PDF (document-service) ----------------------------------
# Cuando el cliente pide una cotización, NO se responde con texto generado:
# se extrae la estructura con el LLM, se genera el PDF en el document-service
# y el canal envía el archivo adjunto real.

QUOTE_TENANT_ID = "chatbot"

QUOTE_EXTRACTION_PROMPT = (
    "Eres el extractor de cotizaciones de un negocio. "
    "Respondes ÚNICAMENTE con un objeto JSON, sin texto adicional.\n"
    "Hoy es {today} (formato AAAA-MM-DD). Si el cliente pide la cotización "
    "para una fecha relativa a hoy (p. ej. \"pasado mañana\", \"mañana\", "
    "\"este sábado\"), calcula la fecha real del evento/delivery y ponla en "
    "el campo \"event_date\" (AAAA-MM-DD). Déjalo vacío si no menciona fecha.\n"
    "El INTENT se decide SOLO por el último mensaje del cliente ({message}). "
    "Un historial donde antes se pidió o se envió una cotización NO convierte "
    "por sí solo el mensaje nuevo en una petición de cotización.\n"
    "Responde con intent \"quote\" ÚNICAMENTE si el último mensaje:\n"
    "  - pide una cotización o presupuesto nuevo, o\n"
    "  - se refiere a una cotización pedida antes (p. ej. \"mándamela\", "
    "\"re-envíame la cotización\", \"me la envías de nuevo\"), o\n"
    "  - pide que se le envíe el PDF o el archivo adjunto de esa cotización "
    "previa (p. ej. \"me puedes enviar el pdf\", \"me pasas el archivo de la "
    "cotización\", \"reenvíame el pdf\", \"envíeme el archivo\").\n"
    "En cualquier otro caso (saludos, gracias, preguntas sobre productos, "
    "horarios, contacto, etc.), responde exactamente: {\"intent\": \"no\"}.\n"
    "Si el intent es \"quote\", usa esta forma:\n"
    "{\n"
    "  \"intent\": \"quote\",\n"
    "  \"customer\": {\"name\": \"nombre del cliente si se conoce, si no Cliente\", \"phone\": \"teléfono si se conoce, si no cadena vacía\"},\n"
    "  \"currency\": \"moneda indicada (ej. DOP o USD); si no se indica, DOP\",\n"
    "  \"event_date\": \"fecha del evento/delivery si se indica (AAAA-MM-DD), si no cadena vacía\",\n"
    "  \"party_size\": 0,\n"
    "  \"items\": [{\"description\": \"producto o servicio\", \"quantity\": 1, \"unit_price\": 0.0}]\n"
    "}\n"
    "\"party_size\": número de personas/invitados si se indica, si no 0.\n"
    "Reglas de ítems: las cantidades y precios deben salir ÚNICAMENTE de la "
    "información del negocio, del último mensaje o de pedidos anteriores SOLO si "
    "el último mensaje se refiere explícitamente a esa cotización previa. "
    "NO inventes datos ni reutilices ítems de una cotización anterior si el "
    "mensaje no la menciona. Incluye un elemento por cada producto o servicio "
    "pedido. Si es una cotización pero no puedes determinar ítems, devuelve "
    "\"items\": [] (sigue siendo una cotización que se pidió armar).\n\n"
    "--- CONVERSACIÓN RECIENTE ---\n{history}\n--- FIN DE LA CONVERSACIÓN ---\n\n"
    "El mensaje más reciente del cliente es: {message}\n\n"
    "--- INFORMACIÓN DEL NEGOCIO ---\n{context}\n--- FIN DE LA INFORMACIÓN ---"
)


def _parse_quote_json(raw: str) -> dict | None:
    """Extrae y valida el JSON de cotización del texto que devuelve el LLM."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text).rstrip()
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("El LLM no devolvió JSON válido en la extracción: %s", raw[:200])
        return None
    if data.get("intent") != "quote":
        return None

    items: list[dict] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        try:
            quantity = round(float(item.get("quantity", 1)), 4)
        except (TypeError, ValueError):
            continue
        try:
            unit_price = round(float(item.get("unit_price", 0)), 2)
        except (TypeError, ValueError):
            continue
        if not description or quantity <= 0 or unit_price < 0:
            continue
        items.append({
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
        })
    if not items:
        # La cotización se pidió pero el LLM no pudo determinar ítems: se
        # devuelve igual para que el canal responda "en un momento te la envío".
        items = []

    customer = data.get("customer") or {}
    name = str(customer.get("name") or "").strip() or "Cliente"
    phone = str(customer.get("phone") or "").strip()
    currency = str(data.get("currency") or "").strip() or "DOP"

    event_date = ""
    event_date_raw = str(data.get("event_date") or "").strip()
    if event_date_raw:
        try:
            event_date = datetime.date.fromisoformat(event_date_raw).isoformat()
        except ValueError:
            logger.warning("event_date inválido devuelto por el LLM: %s", event_date_raw)
    try:
        party_size = int(data.get("party_size") or 0)
    except (TypeError, ValueError):
        party_size = 0
    if party_size < 0:
        party_size = 0

    return {
        "intent": "quote",
        "customer": {"name": name, "phone": phone},
        "currency": currency,
        "event_date": event_date,
        "party_size": party_size,
        "items": items,
    }


async def _extract_quote_draft(
    message: str, context: str, history: list[dict] | None = None,
    force_quote: bool = False,
) -> dict | None:
    """Pide al LLM el JSON estructurado de la cotización. Devuelve None si el
    cliente no pidió una cotización o si ningún provider logró extraerla.
    Si force_quote es True, el último mensaje se trata como respuesta a las
    preguntas de una cotización ya iniciada (intent obligado a \"quote\")."""
    history_text = "\n".join(
        f"{'Cliente' if m.get('role') == 'user' else 'Bot'}: {m.get('content', '')}"
        for m in (history or [])[-8:]
    ) or "(sin mensajes previos)"
    system = (
        QUOTE_EXTRACTION_PROMPT
        .replace("{message}", message)
        .replace("{context}", context)
        .replace("{history}", history_text)
        .replace("{today}", datetime.date.today().isoformat())
    )
    if force_quote:
        system += (
            "\n\nNota: el cliente está completando una cotización ya iniciada; "
            "el bot le pidió los datos que faltaban y este último mensaje es su "
            "respuesta con esos datos. Por lo tanto el intent DEBE ser \"quote\" "
            "y NO debe salir items vacío: arma el JSON con lo que aporta este "
            "mensaje (nombre, personas, fecha) y la información del negocio. "
            "Si el cliente indicó para cuántas personas (p. ej. 15) o detalles "
            "del producto, incluye el ítem con la cantidad correspondiente. "
            "Si el precio del producto varía según el tamaño o la cantidad de "
            "invitados (p. ej. los cotillones), inclúyelo igual con esa "
            "cantidad y unit_price 0.0 (el proveedor confirmará el precio final)."
        )
    draft = await _extract_quote_with(system, message)
    if draft is None and _looks_like_quote_request(message):
        logger.info("Reintento de extracción (mensaje con señales de cotización)")
        retry = (
            "El cliente pidió explícitamente una cotización. "
            "Obligatorio: responde ÚNICAMENTE con el objeto JSON "
            "{\"intent\": \"quote\", ...} descrito antes.\n\n"
        ) + system
        draft = await _extract_quote_with(retry, message)
    return draft


QUOTE_GATE_KEYWORDS = (
    "cotiza", "presupuest", "cotill", "paquete", "kit",
    "armar", "buil", "proforma", "factura", "documento",
    "mándamela", "mandamela", "envíamela", "enviamela",
    "reenvíamela", "reenviamela",
    "pdf", "archivo", "adjun", "reenvi",
)


def _looks_like_quote_request(message: str) -> bool:
    """Heurística ligera para detectar una solicitud de cotización."""
    low = message.lower()
    return any(kw in low for kw in QUOTE_GATE_KEYWORDS)


async def _extract_quote_with(system: str, message: str) -> dict | None:
    """Recorre los providers probando el prompt indicado y el primero que
    devuelva un JSON de cotización válido gana."""
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]
    for provider in PROVIDERS:
        try:
            raw = await _try_provider(provider, messages)
        except (RateLimitError, APITimeoutError, APIError, ValueError) as exc:
            logger.warning("Extractor de cotización falló en %s: %s", provider.name, exc)
            continue
        draft = _parse_quote_json(raw)
        if draft:
            logger.info("Cotización extraída con %s (%s)", provider.name, provider.model)
            return draft
    logger.info("No se extrajo una cotización de los providers")
    return None


async def _materialize_quote(
    draft: dict, fallback_phone: str = ""
) -> tuple[bytes, str, str] | None:
    """Genera el PDF en el document-service y devuelve (bytes, filename, caption)."""
    customer = dict(draft["customer"])
    if not customer.get("phone"):
        customer["phone"] = fallback_phone or "no indicado"
    items = list(draft["items"])
    extras = []
    if draft.get("party_size"):
        extras.append(f"{draft['party_size']} personas")
    if draft.get("event_date"):
        extras.append(f"evento el {draft['event_date']}")
    if extras:
        note = " · ".join(extras)
        items[0] = dict(items[0])
        items[0]["description"] = f"{items[0]['description']} ({note})"
    try:
        result = await document_service.create_quote(
            request_id=f"chat-{secrets.token_hex(8)}",
            tenant_id=QUOTE_TENANT_ID,
            customer=customer,
            items=items,
            currency=draft["currency"],
        )
    except Exception as exc:
        logger.warning("Error al generar la cotización en document-service: %s", exc)
        return None
    if result is None:
        logger.warning("document-service no generó la cotización")
        return None
    pdf = await document_service.fetch_quote_pdf(result.pdf_url)
    if pdf is None:
        logger.warning("No se pudo descargar el PDF de %s", result.pdf_url)
        return None
    caption = (
        f"Cotización {result.quote_number} ({draft['currency']}, "
        f"{len(items)} ítems)"
        + (f" para el {draft['event_date']}" if draft.get("event_date") else "")
        + (" · precios a confirmar según tamaño" if any(i.get("unit_price", 0) == 0 for i in items) else "")
        + ". Adjuntamos el archivo PDF."
    )
    return pdf, f"{result.quote_number}.pdf", caption


QUOTE_PENDING_MSG = (
    "¡Claro! En un momento te enviaré la cotización en PDF por este chat."
)

QUOTE_CLARIFY_MSG = (
    "¡Claro! Para armarte la cotización en PDF necesito que me confirmes:\n"
    "1. Los productos o servicios y sus cantidades (p. ej. cotillón para 15 "
    "personas, y si quieres básico o premium).\n"
    "2. El nombre a quien va la cotización.\n"
    "3. Si es para una fecha concreta (p. ej. \"pasado mañana\"), dímela y la "
    "calculo yo en la cotización."
)

# conversation_id -> (pdf_bytes, filename) de la última cotización enviada.
# Permite reenviar la cotización cuando el cliente solo pide "mándamela" sin
# repetir los ítems (se reenvía el PDF sin depender de que el LLM rearme el JSON).
_last_quote_pdf: dict[str, tuple[bytes, str]] = {}

# conversation_ids que esperan los datos faltantes de una cotización (el último
# mensaje del bot fue QUOTE_CLARIFY_MSG). La siguiente respuesta del cliente se
# sigue tratando como continuidad de esa cotización aunque no traiga keywords.
# Es estado en memoria: un reinicio lo borra.
_pending_quote_info: set[str] = set()

# --- Respuesta corta para agradecimientos/cierres -----------------------------
# Evita que "gracias" caiga al LLM y "prometa" enviar una cotización que ya se
# envió. Se responde una sola línea, sin coste de llamada, y se limpia el estado
# pendiente de la cotización.
SOCIAL_CLOSE_MSG = "¡De nada! ¿En qué más te ayudo?"
# Solo se considera cierre si TODOS los tokens del mensaje pertenecen al
# vocabulario social (o son muletillas). Cualquier palabra con contenido real
# ("necesito", "cotización", números, "otra") invalida el match y el mensaje
# sigue su flujo normal.
_SOCIAL_CLOSE_TRIGGERS = {
    "gracias", "perfecto", "listo", "dale", "adios", "chao", "hasta", "luego",
    "excelente", "genial",
}
_SOCIAL_CLOSE_ALLOWED = _SOCIAL_CLOSE_TRIGGERS | {
    "de", "nada", "ok", "bueno", "bien", "súper", "super", "muchas", "mil",
    "muchísimas", "muy", "si", "sí", "ahora", "espero", "te", "igual",
}


def _is_social_close(message: str) -> bool:
    tokens = {t for t in re.split(r"\W+", message.lower()) if t}
    if not tokens:
        return False
    if not tokens.intersection(_SOCIAL_CLOSE_TRIGGERS):
        return False
    return tokens.issubset(_SOCIAL_CLOSE_ALLOWED)

# --- Detección de reenvío puro del último PDF --------------------------------
# "envíame el archivo pdf", "reenvíame el pdf", "me pasas el archivo"...
# Verbos que significan reenviar + sustantivo del archivo, y sin indicios de
# que quiera generar una cotización nueva (cantidades, personas, precios...).
_RESEND_VERBS = (
    "enví", "envia", "mand", "reenví", "reenvi", "pasa", "pasame", "dame",
)
_RESEND_NOUNS = (
    "pdf", "archivo", "adjun", "documento", "cotización", "cotizacion",
    "mándamela", "mandamela", "envíamela", "enviamela",
    "reenvíamela", "reenviamela", "mándala", "mandala", "mándamelo",
    "mandamelo", "envíamelo", "enviamelo",
)
_NEW_QUOTE_HINTS = (
    "persona", "tamañ", "cuánt", "cuant", "costo", "preci", "cotill",
    "para", "con", "modific", "ampli",
)


def _is_pure_resend(message: str) -> bool:
    """True si el mensaje solo pide reenviar el archivo ya enviado, sin aportar
    datos de una cotización nueva."""
    low = message.lower()
    if not any(n in low for n in _RESEND_NOUNS):
        return False
    if not any(v in low for v in _RESEND_VERBS):
        return False
    return not any(h in low for h in _NEW_QUOTE_HINTS)


async def _run_quote_flow(
    message: str,
    conversation_id: str,
    fallback_phone: str,
    send_message,   # async (text: str) -> None
    send_document,  # async (pdf: bytes, filename: str, caption: str) -> None
) -> bool:
    """Maneja una solicitud de cotización. Devuelve True si el mensaje era una
    cotización (aunque no pudiera generarse el PDF); False si no lo era y debe
    seguir por el chat normal."""
    if _is_social_close(message):
        logger.info("Mensaje social de cierre; respuesta breve sin LLM")
        _pending_quote_info.discard(conversation_id)
        _append_history(conversation_id, [
            {"role": "user", "content": message},
            {"role": "assistant", "content": SOCIAL_CLOSE_MSG},
        ])
        await send_message(SOCIAL_CLOSE_MSG)
        return True

    pending_quote = conversation_id in _pending_quote_info
    if not _looks_like_quote_request(message) and not pending_quote:
        return False

    if conversation_id in _last_quote_pdf and _is_pure_resend(message):
        pdf, filename = _last_quote_pdf[conversation_id]
        logger.info("Reenvío directo de %s (pedido de reenvío del archivo)", filename)
        _pending_quote_info.discard(conversation_id)
        _append_history(conversation_id, [
            {"role": "user", "content": message},
            {"role": "assistant", "content": f"La cotización {filename} fue reenviada en PDF adjunto."},
        ])
        await send_document(pdf, filename, "Te reenvío la cotización en PDF adjunto.")
        return True

    context = rag.retrieve_context(message)
    history = _conv_histories.get(conversation_id, [])
    draft = await _extract_quote_draft(
        message, context, history, force_quote=pending_quote,
    )
    if draft is None:
        _pending_quote_info.discard(conversation_id)
        return False

    _append_history(conversation_id, [{"role": "user", "content": message}])

    if not draft["items"]:
        stored = _last_quote_pdf.get(conversation_id)
        if stored:
            pdf, filename = stored
            logger.info(
                "Cotización solicitada sin ítems nuevos; se reenvía %s", filename
            )
            _pending_quote_info.discard(conversation_id)
            _append_history(conversation_id, [
                {"role": "assistant", "content": f"La cotización {filename} fue reenviada en PDF adjunto."},
            ])
            await send_document(pdf, filename, "Te reenvío la cotización en PDF adjunto.")
            return True
        logger.info(
            "Cotización pedida sin ítems determinables; se piden los datos faltantes"
        )
        _pending_quote_info.add(conversation_id)
        _append_history(conversation_id, [
            {"role": "assistant", "content": QUOTE_CLARIFY_MSG},
        ])
        await send_message(QUOTE_CLARIFY_MSG)
        return True

    _pending_quote_info.discard(conversation_id)
    attachment = await _materialize_quote(draft, fallback_phone=fallback_phone)
    if attachment is None:
        logger.warning("No se materializó la cotización; se responde pendiente")
        _append_history(conversation_id, [
            {"role": "assistant", "content": QUOTE_PENDING_MSG},
        ])
        await send_message(QUOTE_PENDING_MSG)
        return True

    pdf, filename, caption = attachment
    _last_quote_pdf[conversation_id] = (pdf, filename)
    _append_history(conversation_id, [
        {"role": "assistant", "content": f"La cotización {filename} fue enviada en PDF adjunto."},
    ])
    await send_document(pdf, filename, caption)
    return True


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

    conversation_id = telegram_adapter.build_conversation_id(chat_id)
    logger.info("Telegram de %s: %r", chat_id, message[:120])
    async def send_message(text: str) -> None:
        await telegram_adapter.send_message(chat_id, text)
    async def send_document(pdf: bytes, filename: str, caption: str) -> None:
        await telegram_adapter.send_document(chat_id, pdf, filename, caption)
    if await _run_quote_flow(message, conversation_id, "", send_message, send_document):
        return {"status": "ok", "quote": "sent"}

    request = ChatRequest(
        message=message,
        conversation_id=conversation_id,
    )
    response = await chat(request)

    await send_message(response.reply)
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
    """Procesa un mensaje de WhatsApp: si pide cotización genera el PDF y lo
    envía como archivo; si no, llama a /chat y responde con texto."""
    conversation_id = whatsapp_adapter.build_conversation_id(wa_id)
    logger.info("WhatsApp de %s (wamid %s): %r", wa_id, wamid, message[:120])
    async def send_message(text: str) -> None:
        await whatsapp_adapter.send_message(wa_id, text)
    async def send_document(pdf: bytes, filename: str, caption: str) -> None:
        await whatsapp_adapter.send_document(wa_id, pdf, filename, caption)
    if await _run_quote_flow(
        message, conversation_id, wa_id, send_message, send_document
    ):
        return

    request = ChatRequest(
        message=message,
        conversation_id=conversation_id,
    )
    try:
        response = await chat(request)
    except HTTPException as exc:
        logger.error("Error al responder por WhatsApp (wamid %s): %s", wamid, exc.detail)
        return
    await send_message(response.reply)
