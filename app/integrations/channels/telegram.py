"""
Adapter de Telegram para el chatbot.

Funciones auxiliares para procesar updates de Telegram: validar y extraer el
mensaje y el chat_id, generar un conversation_id estable por usuario
(formato "telegram:<chat_id>") y enviar respuestas mediante la Bot API.

No sustituye ninguna funcionalidad existente: es una capa adicional sobre /chat.
El orquestado del flujo (llamar a /chat y responder) lo hace el endpoint de
webhook en main.py.
"""

import logging
import re

import httpx

from app.core import config

logger = logging.getLogger("chatbot.telegram")
CHANNEL_NAME = "telegram"
TELEGRAM_API = "https://api.telegram.org"


def build_conversation_id(chat_id: int) -> str:
    """Genera un conversation_id estable y único por chat de Telegram."""
    return f"{CHANNEL_NAME}:{chat_id}"


def parse_message(update: dict) -> str | None:
    """Extrae el texto del mensaje del update de Telegram. Devuelve None si no hay."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    text = message.get("text")
    if text is None:
        return None
    return text.strip()


def get_chat_id(update: dict) -> int | None:
    """Extrae el chat_id del update. Devuelve None si no se puede identificar."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    chat = message.get("chat")
    if not chat:
        return None
    return chat.get("id")


async def send_message(chat_id: int, text: str) -> bool:
    """Envía un mensaje al chat de Telegram mediante la Bot API."""
    bot_token = config.env("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.warning("No se configuró TELEGRAM_BOT_TOKEN; no se puede responder.")
        return False
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    formatted = _format_for_telegram(text)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url, json={"chat_id": chat_id, "text": formatted[:4096], "parse_mode": "Markdown"}
        )
        if resp.status_code == 200:
            return True
        if resp.status_code != 400:
            return False
        logger.warning("Formato de Telegram rechazado; reenvío sin formato")
        resp = await client.post(url, json={"chat_id": chat_id, "text": text[:4096]})
        return resp.status_code == 200


def _format_for_telegram(text: str) -> str:
    """Convierte Markdown estándar del LLM al formato Telegram Markdown."""
    text = re.sub("\\*\\*(.+?)\\*\\*", "*\\1*", text)
    text = re.sub("^(\\s*)- ", "\\1• ", text, flags=re.MULTILINE)
    return text


async def send_document(chat_id: int, file_bytes: bytes, filename: str, caption: str = "") -> bool:
    """Envía un archivo PDF como documento a un chat de Telegram (sendDocument)."""
    bot_token = config.env("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.warning("No se configuró TELEGRAM_BOT_TOKEN; no se puede enviar el documento.")
        return False
    url = f"{TELEGRAM_API}/bot{bot_token}/sendDocument"
    data: dict = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    files = {"document": (filename, file_bytes, "application/pdf")}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, data=data, files=files)
        if resp.status_code != 200:
            logger.warning(
                "Fallo al enviar el documento por Telegram (%s); respuesta omitida",
                resp.status_code,
            )
    return resp.status_code == 200
