"""Selección del adaptador de canal."""

import logging

from app.integrations.channels import messenger, telegram, whatsapp

logger = logging.getLogger("chatbot")


def channel_adapter(channel: str):
    return {"telegram": telegram, "whatsapp": whatsapp, "messenger": messenger}[channel]
