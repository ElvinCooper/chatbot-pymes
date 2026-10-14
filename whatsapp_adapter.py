"""
Adapter de WhatsApp Business (Cloud API de Meta) para el chatbot.

Funciones auxiliares para procesar webhooks de Meta: validar la firma
X-Hub-Signature-256, extraer los mensajes entrantes, generar un
conversation_id estable por usuario (formato "whatsapp:<wa_id>") y enviar
respuestas mediante la Graph API.

Al igual que telegram_adapter.py, es una capa adicional sobre /chat: el
orquestado del flujo (llamar a /chat y responder) lo hace el endpoint de
webhook en main.py.

Notas de Meta:
  - El handshake de verificación (GET) usa hub.mode / hub.verify_token /
    hub.challenge; el verify_token es DISTINTO del App Secret.
  - Cada POST de evento firma el body crudo con HMAC-SHA256 (App Secret)
    en la cabecera X-Hub-Signature-256.
  - Solo se pueden enviar mensajes de texto libre dentro de la ventana de
    24h tras el último mensaje del cliente; fuera de ella hacen falta
    plantillas aprobadas.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import metrics

logger = logging.getLogger("chatbot.whatsapp")

CHANNEL_NAME = "whatsapp"
GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
MAX_BODY_CHARS = 4096

# Las entregas de webhook con mucho tiempo de vida se descartan: son retries de
# Meta de mensajes ya respondidos (o muy viejos) tras caídas del bot.
STALE_MESSAGE_SECONDS = 900

UNSUPPORTED_MESSAGE_TYPES = (
    "audio",
    "button",
    "contacts",
    "document",
    "image",
    "location",
    "reaction",
    "sticker",
    "video",
)


@dataclass
class InboundMessage:
    """Un mensaje de texto entrante de WhatsApp con lo que mide la Fase 2.

    Además del texto, conserva los metadatos necesarios para registrar el
    evento `message_received` (wamid, remitente, tipo, timestamp de Meta y el
    `phone_number_id` del número que recibió el mensaje).
    """

    wamid: str
    wa_id: str
    text: str
    message_type: str = "text"
    timestamp: int | None = None
    phone_number_id: str | None = None


@dataclass
class MessageStatus:
    """Estado de entrega de un mensaje saliente (Fase 3).

    Meta envía los cambios de estado en `value.statuses[]` (sent, delivered,
    read, failed). El bloque `pricing` viaja en el propio status y trae la
    categoría de facturación (service/utility/marketing/authentication), si es
    facturable y el tipo (regular/free_customer_service/...). Esos datos son la
    base del costeo de WhatsApp.
    """

    wamid: str
    status: str
    timestamp: int | None = None
    recipient_id: str | None = None
    phone_number_id: str | None = None
    pricing_category: str | None = None
    pricing_billable: bool | None = None
    pricing_type: str | None = None
    country_code: str | None = None


def _tenant_id() -> str:
    """Tenant en uso. Misma convención que main.py (default "chatbot")."""
    return os.getenv("TENANT_ID") or "chatbot"


def _utc_iso(epoch_seconds: int | None) -> str:
    """Convierte un timestamp epoch de Meta a ISO 8601 UTC (con fallback a ahora)."""
    if epoch_seconds:
        try:
            return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_conversation_id(wa_id: str) -> str:
    """Genera un conversation_id estable y único por usuario de WhatsApp."""
    return f"{CHANNEL_NAME}:{wa_id}"


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Valida la firma X-Hub-Signature-256 de Meta (HMAC-SHA256 del body crudo)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256=") :]
    return hmac.compare_digest(expected, provided)


def parse_messages(update: dict) -> list[InboundMessage]:
    """Extrae los mensajes de texto entrantes.

    Los updates de Meta vienen anidados como
    entry[].changes[].value.messages[]. Devuelve una lista de `InboundMessage`
    (wamid, wa_id, texto y metadatos). Ignora statuses, ecos de envío propio y
    tipos de mensaje no soportados.
    """
    messages: list[InboundMessage] = []
    for entry in update.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # El phone_number_id del número que recibió el mensaje vive en el
            # metadata del payload; se captura para las métricas (el envío usa
            # el WHATSAPP_PHONE_NUMBER_ID del archivo .env, no este valor).
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            for message in value.get("messages", []):
                wamid = message.get("id", "")
                wa_id = message.get("from", "")
                if message.get("direction") == "outbound":
                    logger.info(
                        "Mensaje de WhatsApp ignorado (eco de envío propio, wamid %s)",
                        wamid,
                    )
                    continue
                if message.get("type") != "text":
                    logger.info(
                        "Mensaje de WhatsApp ignorado (tipo '%s', wamid %s)",
                        message.get("type"),
                        wamid,
                    )
                    continue
                text = (message.get("text") or {}).get("body", "").strip()
                if not text or not wa_id:
                    continue
                try:
                    msg_ts = int(message.get("timestamp") or 0)
                except (TypeError, ValueError):
                    msg_ts = 0
                if msg_ts and (time.time() - msg_ts) > STALE_MESSAGE_SECONDS:
                    logger.info(
                        "Mensaje antiguo ignorado (timestamp %s, wamid %s): retry de Meta",
                        msg_ts,
                        wamid,
                    )
                    continue
                messages.append(
                    InboundMessage(
                        wamid=wamid,
                        wa_id=wa_id,
                        text=text,
                        message_type=message.get("type") or "text",
                        timestamp=msg_ts or None,
                        phone_number_id=phone_number_id,
                    )
                )
    return messages


def parse_statuses(update: dict) -> list[MessageStatus]:
    """Extrae los cambios de estado de entrega (`value.statuses[]`).

    Meta anida los statuses igual que los mensajes:
    entry[].changes[].value.statuses[]. Cada status trae el `id` (wamid), el
    estado (`sent`/`delivered`/`read`/`failed`), el timestamp y, cuando aplica,
    el bloque `pricing` con la categoría de facturación. Se ignoran los statuses
    sin wamid. No se aplica el filtro de antigüedad: los retries viejos son
    idempotentes porque el registro es un UPSERT por wamid.
    """
    statuses: list[MessageStatus] = []
    for entry in update.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            for status in value.get("statuses", []) or []:
                wamid = status.get("id", "")
                if not wamid:
                    continue
                try:
                    st_ts = int(status.get("timestamp") or 0)
                except (TypeError, ValueError):
                    st_ts = 0
                pricing = status.get("pricing") or {}
                billable = pricing.get("billable")
                if not isinstance(billable, bool):
                    billable = None
                statuses.append(
                    MessageStatus(
                        wamid=wamid,
                        status=status.get("status") or "",
                        timestamp=st_ts or None,
                        recipient_id=status.get("recipient_id"),
                        phone_number_id=phone_number_id,
                        pricing_category=pricing.get("category") or None,
                        pricing_billable=billable,
                        pricing_type=pricing.get("type") or None,
                        country_code=pricing.get("country_code") or None,
                    )
                )
    return statuses


def record_message_status(status: MessageStatus) -> None:
    """Registra el estado de entrega de un mensaje saliente (best-effort).

    `delivered` se marca también para `read` (leer implica entregado). El
    UPSERT por wamid hace el registro idempotente ante reintentos y estados
    sucesivos del mismo mensaje.
    """
    delivered = status.status in ("delivered", "read")
    failed = status.status == "failed"
    metrics.upsert_message_status(
        wamid=status.wamid,
        tenant_id=_tenant_id(),
        conversation_id=(
            build_conversation_id(status.recipient_id) if status.recipient_id else None
        ),
        contact_id=status.recipient_id,
        phone_number_id=status.phone_number_id,
        status=status.status,
        delivered=delivered,
        failed=failed,
        pricing_category=status.pricing_category,
        pricing_billable=status.pricing_billable,
        pricing_type=status.pricing_type,
        country_code=status.country_code,
        event_time=_utc_iso(status.timestamp),
    )


def record_message_received(inbound: InboundMessage, payload_size: int | None = None) -> None:
    """Registra un evento `message_received` de WhatsApp (best-effort).

    Se llama desde el webhook SOLO para mensajes que pasaron el dedup (wamid
    no visto) y los chequeos del parse (texto válido, no stale, no eco). Un
    wamid duplicado nunca vuelve a generar el evento.
    """
    metrics.record_usage_event(
        tenant_id=_tenant_id(),
        channel=CHANNEL_NAME,
        direction="inbound",
        event_type="message_received",
        conversation_id=build_conversation_id(inbound.wa_id),
        event_time=_utc_iso(inbound.timestamp),
        wamid=inbound.wamid,
        contact_id=inbound.wa_id,
        phone_number_id=inbound.phone_number_id,
        payload_size=payload_size,
        metadata={"message_type": inbound.message_type},
    )


def _format_for_whatsapp(text: str) -> str:
    """Convierte el Markdown del LLM a texto plano de WhatsApp.

    WhatsApp renderiza *bold* solo en algunos casos y de forma poco fiable,
    así que se quitan todas las variantes de asteriscos para que nunca se
    muestren crudos. Se procesan *** primero para que el de ** no deje rastros.
    """
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"\1", text)
    text = re.sub(r"^(\s*)- ", r"\1• ", text, flags=re.MULTILINE)
    return text


def _extract_wamid(resp: httpx.Response) -> str | None:
    """Lee el wamid de la respuesta exitosa de la Graph API (messages[0].id).

    Devuelve None si la respuesta no trae la lista `messages` o no se puede
    parsear (JSON inválido). El wamid vive en messages[].id; el media_id de la
    subida NO es el wamid, así que no se mezclan.
    """
    try:
        data = resp.json()
    except ValueError:
        return None
    messages = (data or {}).get("messages") or []
    return messages[0].get("id") if messages else None


def _record_sent_message(
    wa_id: str,
    wamid: str | None,
    message_type: str,
    payload: dict,
) -> None:
    """Registra un evento `message_sent` de WhatsApp (best-effort, Fase 2)."""
    metrics.record_usage_event(
        tenant_id=_tenant_id(),
        channel=CHANNEL_NAME,
        direction="outbound",
        event_type="message_sent",
        conversation_id=build_conversation_id(wa_id),
        wamid=wamid,
        contact_id=wa_id,
        phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
        payload_size=len(json.dumps(payload).encode("utf-8")),
        metadata={"message_type": message_type},
    )


async def send_message(wa_id: str, text: str) -> bool:
    """Envía un mensaje de texto al usuario mediante la Graph API.

    Registra un evento `message_sent` (con el wamid que devuelve Meta) cuando
    el envío es exitoso; ante un error de Meta no se registra nada.
    """
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not access_token or not phone_number_id:
        logger.warning(
            "Faltan WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID; no se puede responder."
        )
        return False

    body = _format_for_whatsapp(text)[:MAX_BODY_CHARS]
    url = f"{GRAPH_BASE}/{phone_number_id}/messages"
    payload: dict = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.warning("Error al enviar por WhatsApp (%s); respuesta omitida", resp.status_code)
            return False

        wamid = _extract_wamid(resp)
        if not wamid:
            logger.warning("Respuesta de WhatsApp sin wamid fijada: respuesta omitida")
        _record_sent_message(wa_id, wamid, "text", payload)
    return True


async def send_document(wa_id: str, file_bytes: bytes, filename: str, caption: str = "") -> bool:
    """Envía un archivo PDF como documento (tipo 'document') por WhatsApp.

    La Graph API exige dos pasos: subir el media (se obtiene un media_id) y
    luego enviar un mensaje tipo 'document' referenciando ese id. Registra un
    evento `message_sent` con el wamid de la respuesta del envío (no el
    media_id de la subida).
    """
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not access_token or not phone_number_id:
        logger.warning(
            "Faltan WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID; no se puede enviar el documento."
        )
        return False

    api_url = f"{GRAPH_BASE}/{phone_number_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    upload = {
        "messaging_product": (None, "whatsapp"),
        "type": (None, "application/pdf"),
        "file": (filename, file_bytes, "application/pdf"),
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        up = await client.post(f"{api_url}/media", headers=headers, files=upload)
        if up.status_code != 200:
            logger.warning(
                "Fallo al subir el media a WhatsApp (%s); respuesta omitida", up.status_code
            )
            return False
        media_id = (up.json() or {}).get("id")
        if not media_id:
            logger.warning("Respuesta de media de WhatsApp sin id: respuesta omitida")
            return False

        document: dict = {"id": media_id, "filename": filename}
        if caption:
            document["caption"] = caption
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": wa_id,
            "type": "document",
            "document": document,
        }
        resp = await client.post(f"{api_url}/messages", headers=headers, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "Fallo al enviar el documento por WhatsApp (%s); respuesta omitida",
                resp.status_code,
            )
            return False

        wamid = _extract_wamid(resp)
        if not wamid:
            logger.warning("Respuesta de envío de documento sin wamid fijada: respuesta omitida")
        _record_sent_message(wa_id, wamid, "document", payload)
    return True
