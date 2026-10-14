"""Cierre por inactividad recuperable tras reinicio."""

import asyncio
import logging
import time

from starlette.concurrency import run_in_threadpool

import app.persistence.sessions as session_store
from app.core import config, state
from app.integrations.channels import registry
from app.services import sessions

logger = logging.getLogger("chatbot")
CLOSE_FINAL_MSG = (
    "Como no he tenido respuesta, daré por cerrada esta conversación para mantener tus "
    "datos seguros. Si me necesitas de nuevo, solo escribe un mensaje. ¡Que tengas un buen"
    " día!"
)


async def expire_conversation(cid: str):
    async with sessions.conversation_lock(cid):
        last = state.session_activity.get(cid)
        if last is not None and time.monotonic() - last < config.IDLE_CLOSE_SECONDS:
            return
        token = await run_in_threadpool(session_store.claim_expired_session, cid)
        if not token:
            sessions.close_conversation(cid)
            return
        try:
            target = state.idle_target_hint.get(cid)
            channel = cid.split(":", 1)[0]
            if target is None and channel in {"telegram", "whatsapp", "messenger"}:
                target = cid.split(":", 1)[1]
            if target is not None:
                channel = cid.split(":", 1)[0]
                adapter = registry.channel_adapter(channel)
                await asyncio.wait_for(
                    adapter.send_message(
                        int(target) if channel == "telegram" else target, CLOSE_FINAL_MSG
                    ),
                    timeout=15,
                )
        except Exception as exc:
            logger.warning("Fallo de cierre por inactividad: %s", type(exc).__name__)
        finally:
            sessions.close_conversation(cid)
            try:
                await run_in_threadpool(session_store.delete_session, cid)
            finally:
                await run_in_threadpool(session_store.release_session, cid, token)


async def idle_monitor() -> None:
    """Un error no termina el monitor; cierre protegido con una lease distribuida."""
    while True:
        await asyncio.sleep(max(1, config.IDLE_CHECK_INTERVAL))
        configured = config.env("BOT_CHANNEL", "all")
        channels = ("telegram", "whatsapp", "messenger") if configured == "all" else (configured,)
        try:
            expired = await run_in_threadpool(session_store.expired_sessions, channels)
        except Exception as exc:
            logger.warning("Fallo al consultar cierres: %s", type(exc).__name__)
            continue
        for cid in expired:
            try:
                await expire_conversation(cid)
            except Exception as exc:
                logger.warning("Fallo al cerrar sesión expirada: %s", type(exc).__name__)
