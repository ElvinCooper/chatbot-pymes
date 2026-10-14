"""Recepción durable y procesamiento ordenado por canal."""

import asyncio
import base64
import logging
import re

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

import app.schemas.chat as chat_schema
import app.services.chat as chat_service
from app.core import config, resources, state
from app.integrations.channels import registry
from app.persistence import jobs
from app.services import quotes, sessions

logger = logging.getLogger("chatbot")


async def enqueue(channel: str, event_id: str, target: str, message: str) -> dict:
    if (
        not isinstance(event_id, str)
        or not 1 <= len(event_id) <= 200
        or (not re.fullmatch("-?[0-9]{1,30}", target))
    ):
        raise HTTPException(400, "Identificador inválido")
    try:
        message = chat_schema.ChatRequest(message=message).message
    except ValidationError:
        raise HTTPException(422, "Mensaje inválido o demasiado largo") from None
    cid = f"{channel}:{target}"
    try:
        inserted = await run_in_threadpool(
            jobs.enqueue, channel, event_id, cid, {"target": target, "message": message}
        )
    except Exception as exc:
        logger.warning("No se pudo persistir la recepción: %s", type(exc).__name__)
        raise HTTPException(503, "Recepción temporalmente no disponible") from None
    return {"status": "ok"} if inserted else {"status": "ok", "deduplicated": True}


async def deliver_plan(job: dict, adapter, target):
    plan = job["plan"]
    if not plan.get("accepted"):
        if plan["kind"] == "text":
            accepted = await adapter.send_message(target, plan["text"])
        else:
            accepted = await adapter.send_document(
                target, base64.b64decode(plan["pdf"]), plan["filename"], plan["caption"]
            )
        if accepted is not True:
            raise RuntimeError("El canal no confirmó aceptación")
        await run_in_threadpool(jobs.checkpoint, job, {**plan, "accepted": True})


async def handle_channel_message(job: dict):
    cid, channel = (job["conversation"], job["channel"])
    target = job["payload"]["target"]
    target = int(target) if channel == "telegram" else target
    message = job["payload"]["message"]
    adapter = registry.channel_adapter(channel)
    async with sessions.session_guard(cid):
        await resources.reserve(f"contact:{cid}", 20)
        await sessions.restore_session(cid)
        sessions.clear_idle(cid)
        if job.get("plan") and "kind" in job["plan"]:
            await deliver_plan(job, adapter, target)
            plan = job["plan"]
            saved = plan.get("session")
            if saved:
                state.conv_histories[cid] = saved["history"]
                if saved["pending"]:
                    state.pending_quote_info.add(cid)
                else:
                    state.pending_quote_info.discard(cid)
            if plan["kind"] == "pdf":
                sessions.cache_pdf(cid, base64.b64decode(plan["pdf"]), plan["filename"])
            reply = plan.get("text", "Cotización aceptada por la API del canal.")
            if not saved:
                pair = [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": reply},
                ]
                if state.conv_histories.get(cid, [])[-2:] != pair:
                    sessions.append_history(cid, pair)
            elif plan["kind"] == "pdf":
                sessions.append_history(cid, [{"role": "assistant", "content": reply}])
        else:

            def snapshot():
                return {
                    "history": state.conv_histories.get(cid, []),
                    "pending": cid in state.pending_quote_info,
                }

            async def send_message(text: str):
                await run_in_threadpool(
                    jobs.checkpoint, job, {"kind": "text", "text": text, "session": snapshot()}
                )
                await deliver_plan(job, adapter, target)

            async def send_document(pdf: bytes, filename: str, caption: str):
                await run_in_threadpool(
                    jobs.checkpoint,
                    job,
                    {
                        "kind": "pdf",
                        "pdf": base64.b64encode(pdf).decode(),
                        "filename": filename,
                        "caption": caption,
                        "session": snapshot(),
                    },
                )
                await deliver_plan(job, adapter, target)

            token = state.current_job.set(job)
            try:
                if not await quotes.run_quote_flow(
                    message,
                    cid,
                    str(target) if channel == "whatsapp" else "",
                    send_message,
                    send_document,
                ):
                    response = await chat_service.chat(
                        chat_schema.ChatRequest(message=message, conversation_id=cid)
                    )
                    await send_message(response.reply)
            finally:
                state.current_job.reset(token)
        await sessions.persist_session(cid)
        sessions.track_idle(cid, str(target))


async def inbox_worker():
    configured = config.env("BOT_CHANNEL", "all")
    channels = ("telegram", "whatsapp", "messenger") if configured == "all" else (configured,)
    if any((c not in {"telegram", "whatsapp", "messenger"} for c in channels)):
        raise ValueError("BOT_CHANNEL inválido")
    while True:
        try:
            job = await run_in_threadpool(jobs.claim, channels)
            if not job:
                await asyncio.sleep(1)
                continue
            try:
                async with asyncio.timeout(180):
                    await handle_channel_message(job)
            except Exception as exc:
                await run_in_threadpool(jobs.finish, job, type(exc).__name__)
                logger.warning("Trabajo pendiente de reintento: %s", type(exc).__name__)
            else:
                await run_in_threadpool(jobs.finish, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Fallo del worker: %s", type(exc).__name__)
            await asyncio.sleep(2)
