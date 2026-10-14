"""Contratos de respuesta HTTP del chatbot."""

import logging

from pydantic import BaseModel

logger = logging.getLogger("chatbot")


class ChatResponse(BaseModel):
    reply: str
    provider_used: str
    model_used: str
    used_context: bool


class QuoteResponse(BaseModel):
    document_id: str
    quote_number: str
    pdf_url: str
    size_bytes: int
    status: str
