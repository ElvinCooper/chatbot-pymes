"""Estado local y límites de concurrencia del proceso."""

import asyncio
import logging
from contextvars import ContextVar

logger = logging.getLogger("chatbot")
current_job: ContextVar[dict | None] = ContextVar("current_job", default=None)
conversation_locks = [asyncio.Lock() for _ in range(64)]
llm_slots = asyncio.Semaphore(4)
rag_slots = asyncio.Semaphore(2)
CONV_HISTORY_LIMIT = 8
conv_histories: dict[str, list[dict]] = {}
session_activity: dict[str, float] = {}
MAX_SESSIONS = 500
idle_last_activity: dict[str, float] = {}
idle_target_hint: dict[str, str] = {}
last_quote_pdf: dict[str, tuple[bytes, str]] = {}
pending_quote_info: set[str] = set()
