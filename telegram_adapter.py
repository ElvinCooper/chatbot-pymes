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
import os

import httpx

logger = logging.getLogger("chatbot.telegram")

CHANNEL_NAME = "telegram"
TELEGRAM_API = "https://api.telegram.org"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


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


async def send_message(chat_id: int, text: str) -> None:
    """Envía un mensaje al chat de Telegram mediante la Bot API."""
    if not BOT_TOKEN:
        logger.warning("No se configuró TELEGRAM_BOT_TOKEN; no se puede responder.")
        return
    url = f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text})
        resp.raise_for_status()
