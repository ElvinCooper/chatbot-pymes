"""
Adapter de Instagram Messaging API para el chatbot.

Fase inicial:
- Genera un conversation_id estable.
- Extrae mensajes de texto de los webhooks de Instagram.
- De momento no envía respuestas; primero validamos recepción del webhook.
"""

import logging
import time

logger = logging.getLogger("chatbot.instagram")

CHANNEL_NAME = "instagram"

# Ignorar eventos demasiado antiguos.
STALE_MESSAGE_SECONDS = 900


def build_conversation_id(user_id: str) -> str:
    """Genera un conversation_id estable por usuario de Instagram."""
    return f"{CHANNEL_NAME}:{user_id}"


def parse_messages(update: dict) -> list[tuple[str, str, str]]:
    """
    Extrae mensajes de texto de un webhook de Instagram.

    Devuelve:
        (message_id, sender_id, text)

    En esta primera fase solamente aceptamos mensajes de texto.
    """
    messages: list[tuple[str, str, str]] = []

    for entry in update.get("entry", []):
        for event in entry.get("messaging", []):
            sender = event.get("sender") or {}
            message = event.get("message") or {}

            sender_id = sender.get("id", "")
            message_id = message.get("mid", "")
            text = (message.get("text") or "").strip()

            if not sender_id or not text:
                continue

            timestamp = event.get("timestamp")
            try:
                event_ts = float(timestamp) / 1000 if timestamp else 0
            except (TypeError, ValueError):
                event_ts = 0

            if event_ts and (time.time() - event_ts) > STALE_MESSAGE_SECONDS:
                logger.info(
                    "Mensaje antiguo de Instagram ignorado (mid %s)",
                    message_id,
                )
                continue

            messages.append((message_id, sender_id, text))

    return messages
