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
import logging
import os
import re

import httpx

logger = logging.getLogger("chatbot.whatsapp")

CHANNEL_NAME = "whatsapp"
GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
MAX_BODY_CHARS = 4096

UNSUPPORTED_MESSAGE_TYPES = ("audio", "button", "contacts", "document", "image",
                             "location", "reaction", "sticker", "video")


def build_conversation_id(wa_id: str) -> str:
    """Genera un conversation_id estable y único por usuario de WhatsApp."""
    return f"{CHANNEL_NAME}:{wa_id}"


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Valida la firma X-Hub-Signature-256 de Meta (HMAC-SHA256 del body crudo)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


def parse_messages(update: dict) -> list[tuple[str, str, str]]:
    """Extrae los mensajes de texto entrantes.

    Los updates de Meta vienen anidados como
    entry[].changes[].value.messages[]. Devuelve una lista de tuplas
    (wamid, wa_id, texto). Ignora statuses y tipos de mensaje no soportados.
    """
    messages: list[tuple[str, str, str]] = []
    for entry in update.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
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
                        message.get("type"), wamid,
                    )
                    continue
                text = (message.get("text") or {}).get("body", "").strip()
                if not text or not wa_id:
                    continue
                messages.append((wamid, wa_id, text))
    return messages


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


async def send_message(wa_id: str, text: str) -> None:
    """Envía un mensaje de texto al usuario mediante la Graph API."""
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not access_token or not phone_number_id:
        logger.warning(
            "Faltan WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID; no se puede responder."
        )
        return

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
            logger.warning(
                "Error al enviar por WhatsApp (%s): %s", resp.status_code, resp.text
            )


async def send_document(wa_id: str, file_bytes: bytes, filename: str, caption: str = "") -> None:
    """Envía un archivo PDF como documento (tipo 'document') por WhatsApp.

    La Graph API exige dos pasos: subir el media (se obtiene un media_id) y
    luego enviar un mensaje tipo 'document' referenciando ese id.
    """
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not access_token or not phone_number_id:
        logger.warning(
            "Faltan WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID; no se puede enviar el documento."
        )
        return

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
                "Fallo al subir el media a WhatsApp (%s): %s", up.status_code, up.text
            )
            return
        media_id = (up.json() or {}).get("id")
        if not media_id:
            logger.warning("Respuesta de media de WhatsApp sin id: %s", up.text)
            return

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
                "Fallo al enviar el documento por WhatsApp (%s): %s",
                resp.status_code, resp.text,
            )