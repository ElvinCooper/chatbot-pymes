"""Estado de conversación, persistencia y exclusión entre procesos."""

import asyncio
import base64
import hashlib
import logging
import time
from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

import app.persistence.sessions as session_store
from app.core import config, state

logger = logging.getLogger("chatbot")


def conversation_lock(conversation_id: str):
    return state.conversation_locks[
        int(hashlib.sha256(conversation_id.encode()).hexdigest(), 16) % 64
    ]


@asynccontextmanager
async def session_guard(cid: str):
    """Lease entre procesos; timeout menor que la lease evita escritores simultáneos."""
    async with conversation_lock(cid):
        token = None
        for _ in range(20):
            token = await run_in_threadpool(session_store.acquire_session, cid)
            if token:
                break
            await asyncio.sleep(0.5)
        if not token:
            raise HTTPException(429, "La conversación está ocupada")
        try:
            async with asyncio.timeout(180):
                yield
        finally:
            await run_in_threadpool(session_store.release_session, cid, token)


def track_idle(conversation_id: str, target_hint: str) -> None:
    """Registra que el bot acaba de responder: arranca el contador de inactividad."""
    state.idle_last_activity[conversation_id] = time.monotonic()
    state.idle_target_hint[conversation_id] = target_hint


def clear_idle(conversation_id: str) -> None:
    """Pausa el contador de inactividad (el cliente acaba de escribir)."""
    state.idle_last_activity.pop(conversation_id, None)
    state.idle_target_hint.pop(conversation_id, None)


def close_conversation(conversation_id: str) -> None:
    """Limpia toda la sesión de una conversación cerrada por inactividad."""
    state.session_activity.pop(conversation_id, None)
    state.conv_histories.pop(conversation_id, None)
    state.pending_quote_info.discard(conversation_id)
    state.last_quote_pdf.pop(conversation_id, None)
    state.idle_last_activity.pop(conversation_id, None)
    state.idle_target_hint.pop(conversation_id, None)


def append_history(conversation_id: str | None, messages: list[dict]) -> None:
    if not conversation_id:
        return
    touch_session(conversation_id)
    hist = state.conv_histories.setdefault(conversation_id, [])
    hist.extend(messages)
    state.conv_histories[conversation_id] = hist[-state.CONV_HISTORY_LIMIT :]


def channel_from_conversation(conversation_id: str | None) -> str:
    """Devuelve el canal real si el conversation_id lleva prefijo (whatsapp:,
    messenger:, telegram:, instagram:); si no, "api" (por ejemplo /chat directo)."""
    if conversation_id:
        for channel in ("whatsapp", "messenger", "telegram", "instagram"):
            if conversation_id.startswith(f"{channel}:"):
                return channel
    return "api"


def touch_session(cid: str):
    now = time.monotonic()
    for stale, last in list(state.session_activity.items()):
        if now - last > config.IDLE_CLOSE_SECONDS and stale not in state.idle_target_hint:
            close_conversation(stale)
    if cid not in state.session_activity and len(state.session_activity) >= state.MAX_SESSIONS:
        raise HTTPException(503, "Capacidad de sesiones agotada")
    state.session_activity[cid] = now


def session_data(cid: str) -> dict:
    pdf = state.last_quote_pdf.get(cid)
    return {
        "history": state.conv_histories.get(cid, []),
        "pending": cid in state.pending_quote_info,
        "pdf": base64.b64encode(pdf[0]).decode() if pdf else None,
        "filename": pdf[1] if pdf else None,
    }


def cache_pdf(cid: str, pdf: bytes, filename: str):
    state.last_quote_pdf.pop(cid, None)
    while (
        state.last_quote_pdf
        and sum((len(value[0]) for value in state.last_quote_pdf.values())) + len(pdf)
        > 20 * 1024 * 1024
    ):
        state.last_quote_pdf.pop(next(iter(state.last_quote_pdf)))
    if len(pdf) <= 5 * 1024 * 1024:
        state.last_quote_pdf[cid] = (pdf, filename)


async def persist_session(cid: str):
    await run_in_threadpool(
        session_store.save_session, cid, session_data(cid), config.IDLE_CLOSE_SECONDS
    )


async def restore_session(cid: str):
    stored = await run_in_threadpool(session_store.load_session, cid)
    close_conversation(cid)
    touch_session(cid)
    if stored:
        state.conv_histories[cid] = stored.get("history", [])[-state.CONV_HISTORY_LIMIT :]
        if stored.get("pending"):
            state.pending_quote_info.add(cid)
        if stored.get("pdf"):
            cache_pdf(cid, base64.b64decode(stored["pdf"]), stored["filename"])
