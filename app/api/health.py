"""Estado público de la aplicación."""

import logging

from fastapi import APIRouter

from app.services import llm

router = APIRouter()
logger = logging.getLogger("chatbot.api")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "providers_configured": bool(any((p.api_key for p in llm.PROVIDERS)))}
