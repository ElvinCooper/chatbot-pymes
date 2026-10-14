"""Chat autenticado y aislamiento de identidad."""

import logging

from fastapi import APIRouter

import app.schemas.chat as chat_schema
import app.services.chat as chat_service
from app.core import resources, security
from app.schemas import responses
from app.services import sessions

router = APIRouter()
logger = logging.getLogger("chatbot.api")


@router.post("/chat", response_model=responses.ChatResponse)
async def chat(
    request: chat_schema.ChatRequest, identity: security.IdentityDep
) -> responses.ChatResponse:
    await resources.reserve(f"api:{identity.client_id}", 20)
    cid = security.api_conversation(identity, request.conversation_id)
    async with sessions.session_guard(cid or identity.client_id):
        if cid:
            await sessions.restore_session(cid)
        response = await chat_service.chat(request.model_copy(update={"conversation_id": cid}))
        if cid:
            await sessions.persist_session(cid)
        return response
