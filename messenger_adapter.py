"""
Adapter de Facebook Messenger (Messenger Platform de Meta) para el chatbot.

Funciones auxiliares para procesar webhooks de Meta: validar la firma
X-Hub-Signature-256, extraer los mensajes entrantes, generar un
conversation_id estable por usuario (formato "messenger:<psid>") y enviar
respuestas mediante la Graph API.

Al igual que telegram_adapter.py y whatsapp_adapter.py, es una capa adicional
sobre /chat: el orquestado del flujo (llamar a /chat y responder) lo hace el
endpoint de webhook en main.py.

Notas de Meta:
  - El handshake de verificación (GET) usa hub.mode / hub.verify_token /
    hub.challenge; el verify_token es DISTINTO del App Secret.
  - Cada POST de evento firma el body crudo con HMAC-SHA256 (App Secret)
    en la cabecera X-Hub-Signature-256.
  - A diferencia de WhatsApp, los mensajes llegan en entry[].messaging[] (igual
    que Instagram) y el timestamp viene en MILISEGUNDOS.
  - Meta reenvía nuestros propios mensajes como eco: llegan marcados con
    message.is_echo = true, con sender.id = PAGE_ID, y en el campo
    message_echoes. Hay que descartarlos o el bot se responde a sí mismo.
  - Solo se pueden enviar mensajes con messaging_type RESPONSE dentro de la
    ventana de 24h tras el último mensaje del cliente.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time

import httpx

logger = logging.getLogger("chatbot.messenger")

CHANNEL_NAME = "messenger"
GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Messenger limita el texto a 2000 caracteres por mensaje (WhatsApp admite 4096).
MAX_TEXT_CHARS = 2000

# Las entregas de webhook con mucho tiempo de vida se descartan: son retries de
# Meta de mensajes ya respondidos (o muy viejos) tras caídas del bot.
STALE_MESSAGE_SECONDS = 900


def build_conversation_id(psid: str) -> str:
    """Genera un conversation_id estable y único por usuario de Messenger.

    El psid es el Page-Scoped ID del usuario, no su teléfono, así que el
    conversation_id no se puede usar como número de contacto.
    """
    return f"{CHANNEL_NAME}:{psid}"


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str | None) -> bool:
    """Valida la firma X-Hub-Signature-256 de Meta (HMAC-SHA256 del body crudo).

    Sin app_secret no se puede validar nada: se devuelve False en vez de
    lanzar (el llamante decide si rechaza o deja pasar).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not app_secret:
        return False
    expected = hmac.new(
        app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


def _is_echo(event: dict) -> bool:
    """Detecta si el evento es un eco de un mensaje que envió la propia Página.

    Los ecos se marcan con message.is_echo y su sender.id es el PAGE_ID (no el
    del usuario). Ambos se comprueban porque Meta no garantiza los dos.
    """
    message = event.get("message") or {}
    if message.get("is_echo"):
        return True
    page_id = os.getenv("MESSENGER_PAGE_ID")
    sender_id = (event.get("sender") or {}).get("id")
    return bool(page_id) and bool(sender_id) and sender_id == page_id


def parse_messages(update: dict) -> list[tuple[str, str, str]]:
    """Extrae los mensajes de texto entrantes.

    A diferencia de WhatsApp, los updates llegan como entry[].messaging[].
    Devuelve una lista de tuplas (mid, psid, texto). Ignora ecos, mensajes de
    otros tipos (adjuntos, quick replies, stickers) y entregas muy antiguas.
    """
    messages: list[tuple[str, str, str]] = []
    for entry in update.get("entry", []):
        for event in entry.get("messaging", []):
            if _is_echo(event):
                logger.info(
                    "Mensaje de Messenger ignorado (eco de la Página, mid %s)",
                    (event.get("message") or {}).get("mid"),
                )
                continue
            parsed = _parse_event(event)
            if parsed:
                messages.append(parsed)
        # Todo lo que llega en message_echoes es, por definición, un eco de un
        # mensaje que envió la Página: se descarta sin mirar los flags.
        for event in entry.get("message_echoes", []):
            logger.info(
                "Mensaje de Messenger ignorado (eco en message_echoes, mid %s)",
                (event.get("message") or {}).get("mid"),
            )
    return messages


def _parse_event(event: dict) -> tuple[str, str, str] | None:
    """Normaliza un evento de entry[].messaging[] a (mid, psid, texto), o None
    si no es un mensaje de texto utilizable."""
    sender = event.get("sender") or {}
    message = event.get("message") or {}

    psid = sender.get("id", "")
    mid = message.get("mid", "")
    text = (message.get("text") or "").strip()

    if not text:
        if message.get("attachments"):
            logger.info(
                "Mensaje de Messenger ignorado (adjunto sin texto, mid %s)", mid
            )
        else:
            logger.info("Mensaje de Messenger ignorado (sin texto, mid %s)", mid)
        return None
    if not psid or not mid:
        return None

    # El timestamp de Messenger viene en milisegundos, no en segundos.
    timestamp = event.get("timestamp")
    try:
        event_ts = float(timestamp) / 1000 if timestamp else 0
    except (TypeError, ValueError):
        event_ts = 0
    if event_ts and (time.time() - event_ts) > STALE_MESSAGE_SECONDS:
        logger.info(
            "Mensaje antiguo de Messenger ignorado (mid %s): retry de Meta", mid
        )
        return None

    return (mid, psid, text)


def _format_for_messenger(text: str) -> str:
    """Convierte el Markdown del LLM a texto plano de Messenger.

    Messenger no interpreta Markdown, así que se quitan todas las variantes de
    asteriscos para que nunca se muestren crudos. Se procesan *** primero para
    que el de ** no deje rastros.
    """
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"\1", text)
    text = re.sub(r"^(\s*)- ", r"\1• ", text, flags=re.MULTILINE)
    return text


def _split_text(text: str, limit: int = MAX_TEXT_CHARS) -> list[str]:
    """Trocea un texto largo en trozos de hasta `limit` caracteres.

    Prefiere cortar en párrafos, luego líneas y luego espacios, para no partir
    palabras por la mitad. El corte es duro solo si un trozo no tiene ningún
    punto de corte posible.

    La unión de los trozos es EXACTAMENTE el texto original: si no lo fuera,
    dos palabras quedarían pegadas en el límite entre mensajes ("palabrapalabra").
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = limit
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > 0:
                # El separador se queda al final del trozo actual (visiblemente
                # invisible en el caso del espacio) y no en el siguiente.
                cut = min(idx + len(sep), limit)
                break
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


async def _post_message(page_id: str, access_token: str, psid: str, message: dict) -> bool:
    """Envía un objeto `message` (texto y/o adjunto) a un psid. Devuelve OK/ko."""
    payload = {
        "recipient": {"id": psid},
        "messaging_type": "RESPONSE",
        "message": message,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{page_id}/messages", json=payload, headers=headers
        )
        if resp.status_code != 200:
            logger.warning(
                "Error al enviar por Messenger (%s): %s", resp.status_code, resp.text
            )
            return False
    return True


def _credentials(action: str) -> tuple[str, str] | None:
    """Devuelve (access_token, page_id) o None si falta alguno.

    El warning nombra SOLO las variables que realmente falten: decir "faltan
    las dos" cuando solo falta una complica depurar desde el log.
    """
    access_token = os.getenv("MESSENGER_ACCESS_TOKEN")
    page_id = os.getenv("MESSENGER_PAGE_ID")
    missing = [
        name
        for name, value in (
            ("MESSENGER_ACCESS_TOKEN", access_token),
            ("MESSENGER_PAGE_ID", page_id),
        )
        if not value
    ]
    if missing:
        logger.warning("Falta %s; no se puede %s.", ", ".join(missing), action)
        return None
    return access_token or "", page_id or ""


async def send_message(psid: str, text: str) -> None:
    """Envía un mensaje de texto al usuario mediante la Graph API.

    Si el texto supera MAX_TEXT_CHARS se envía troceado en varios mensajes, ya
    que la API rechaza cuerpos de más de 2000 caracteres.
    """
    creds = _credentials("responder por Messenger")
    if creds is None:
        return
    access_token, page_id = creds

    body = _format_for_messenger(text)
    for chunk in _split_text(body):
        await _post_message(page_id, access_token, psid, {"text": chunk})


async def send_document(psid: str, file_bytes: bytes, filename: str, caption: str = "") -> None:
    """Envía un PDF como archivo (tipo 'file') por Messenger.

    La Graph API exige dos pasos: subir el archivo (se obtiene un attachment_id)
    y luego enviar un mensaje tipo 'file' referenciando ese id.

    Diferencia con WhatsApp: Messenger NO admite caption en los adjuntos, así
    que el texto de `caption` (que puede llevar datos relevantes del PDF) se
    envía antes como mensaje de texto independiente.
    """
    creds = _credentials("enviar el documento")
    if creds is None:
        return
    access_token, page_id = creds

    if caption:
        await send_message(psid, caption)

    api_url = f"{GRAPH_BASE}/{page_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    # El campo `message` del upload es un JSON serializado dentro del multipart.
    form = {
        "message": json.dumps(
            {"attachment": {"type": "file", "payload": {"is_reusable": True}}}
        )
    }
    files = {"filedata": (filename, file_bytes, "application/pdf")}

    async with httpx.AsyncClient(timeout=60.0) as client:
        up = await client.post(
            f"{api_url}/message_attachments", headers=headers, data=form, files=files
        )
        if up.status_code != 200:
            logger.warning(
                "Fallo al subir el archivo a Messenger (%s): %s", up.status_code, up.text
            )
            return
        attachment_id = (up.json() or {}).get("id")
        if not attachment_id:
            logger.warning(
                "Respuesta de message_attachments de Messenger sin id: %s", up.text
            )
            return

        message = {
            "attachment": {
                "type": "file",
                "payload": {"attachment_id": attachment_id},
            }
        }
        resp = await client.post(
            f"{api_url}/messages",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": message,
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "Fallo al enviar el documento por Messenger (%s): %s",
                resp.status_code, resp.text,
            )