"""Rutas HTTP de webhooks"""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

import app.workers.inbox as inbox_worker
from app.core import config, resources, security
from app.integrations.channels import instagram, messenger, telegram, whatsapp

router = APIRouter()
logger = logging.getLogger("chatbot.api")


@router.get("/webhooks/instagram")
async def instagram_webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Handshake de verificación del webhook de Instagram."""
    if not config.INSTAGRAM_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="INSTAGRAM_VERIFY_TOKEN no configurado")
    if hub_mode == "subscribe" and hmac.compare_digest(
        hub_verify_token or "", config.INSTAGRAM_VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verify token de Instagram inválido")


@router.post("/webhooks/instagram")
async def instagram_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Recibe eventos de Instagram."""
    raw_body = await request.body()
    security.validate_meta(
        raw_body, request.headers.get("X-Hub-Signature-256"), config.INSTAGRAM_APP_SECRET
    )
    update = security.parse_webhook(raw_body)
    await resources.reserve("webhook:instagram", 120)
    try:
        instagram.parse_messages(update)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(400, "Payload inválido") from None
    return {"status": "ok"}


@router.post("/webhooks/telegram")
async def telegram_webhook(
    update: dict,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict:
    security.verify_webhook_secret(x_telegram_bot_api_secret_token)
    await resources.reserve("webhook:telegram", 120)
    try:
        chat_id = telegram.get_chat_id(update)
        message = telegram.parse_message(update)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(400, "Payload inválido") from None
    if chat_id is None or message is None:
        return {"status": "ok", "ignored": True}
    if (
        not isinstance(chat_id, int)
        or isinstance(chat_id, bool)
        or (not isinstance(update.get("update_id"), int))
    ):
        raise HTTPException(400, "Identificador inválido")
    return await inbox_worker.enqueue("telegram", str(update["update_id"]), str(chat_id), message)


@router.get("/webhooks/whatsapp")
async def whatsapp_webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Handshake de verificación de Meta. Debe responder con el challenge
    CRUDO (texto plano, no JSON) si el verify_token coincide."""
    if not config.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="WHATSAPP_VERIFY_TOKEN no configurado")
    if hub_mode == "subscribe" and hmac.compare_digest(
        hub_verify_token or "", config.WHATSAPP_VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verify token inválido")


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Recibe los events de WhatsApp. Lee el body crudo para validar la firma
    y procesa la conversación en background para responder 200 rápido (Meta
    reintenta si tarda o falla)."""
    if not config.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="WHATSAPP_VERIFY_TOKEN no configurado")
    raw_body = await request.body()
    security.validate_meta(
        raw_body, request.headers.get("X-Hub-Signature-256"), config.WHATSAPP_APP_SECRET
    )
    update = security.parse_webhook(raw_body)
    await resources.reserve("webhook:whatsapp", 120)
    try:
        messages = whatsapp.parse_messages(update)
        statuses = whatsapp.parse_statuses(update)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(400, "Payload inválido") from None
    for msg in messages:
        result = await inbox_worker.enqueue("whatsapp", msg.wamid, msg.wa_id, msg.text)
        if not result.get("deduplicated"):
            whatsapp.record_message_received(msg, payload_size=len(raw_body))
    for status in statuses:
        whatsapp.record_message_status(status)
    return {"status": "ok"}


@router.get("/webhooks/messenger")
async def messenger_webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Handshake de verificación de Meta. Debe responder con el challenge
    CRUDO (texto plano, no JSON) si el verify_token coincide."""
    if not config.MESSENGER_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="MESSENGER_VERIFY_TOKEN no configurado")
    if hub_mode == "subscribe" and hmac.compare_digest(
        hub_verify_token or "", config.MESSENGER_VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verify token inválido")


@router.post("/webhooks/messenger")
async def messenger_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    """Recibe los eventos de Messenger. Lee el body crudo para validar la firma
    y procesa la conversación en background para responder 200 rápido (Meta
    reintenta si tarda o falla)."""
    if not config.MESSENGER_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="MESSENGER_VERIFY_TOKEN no configurado")
    raw_body = await request.body()
    security.validate_meta(
        raw_body, request.headers.get("X-Hub-Signature-256"), config.MESSENGER_APP_SECRET
    )
    update = security.parse_webhook(raw_body)
    await resources.reserve("webhook:messenger", 120)
    try:
        messages = messenger.parse_messages(update)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(400, "Payload inválido") from None
    for mid, psid, message in messages:
        await inbox_worker.enqueue("messenger", mid, psid, message)
    return {"status": "ok"}
