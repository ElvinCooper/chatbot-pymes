"""Operaciones RAG y reservas de consumo compartidas."""

import logging

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.core import state
from app.persistence import quotas

logger = logging.getLogger("chatbot")


async def rag_call(function, *args, **kwargs):
    async with state.rag_slots:
        return await run_in_threadpool(function, *args, **kwargs)


async def reserve(scope: str, limit: int, window: int = 60, amount: int = 1):
    try:
        await run_in_threadpool(quotas.reserve, scope, limit, window, amount)
    except RuntimeError:
        raise HTTPException(429, "Cuota agotada", headers={"Retry-After": "60"}) from None
    except Exception:
        raise HTTPException(503, "No se pudo verificar la cuota") from None
