"""Proveedores LLM y fallback, con medición real del proveedor."""

import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core import config, resources, state

logger = logging.getLogger("chatbot")


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str | None
    model: str


@dataclass
class LLMResult:
    """Respuesta de un proveedor LLM con la medición de tokens (Fase 0).

    Los tokens vienen del `usage` del proveedor que realmente respondió; si el
    proveedor no reporta usage quedan en None (NULL en la BD de métricas) para
    distinguir "no disponible" de "cero tokens". Nunca se estiman a mano.
    """

    text: str
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


PROVIDERS: list[Provider] = [
    Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key=config.env("GROQ_API_KEY"),
        model="openai/gpt-oss-120b",
    ),
    Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=config.env("OPENROUTER_API_KEY"),
        model="meta-llama/llama-3.3-70b-instruct",
    ),
    Provider(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=config.env("GEMINI_API_KEY"),
        model="gemini-3.6-flash",
    ),
]


async def try_provider(provider: Provider, messages: list[dict]) -> LLMResult:
    if not provider.api_key:
        raise ValueError(f"{provider.name}: falta API key")
    reserve_tokens = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 1000
    await resources.reserve("llm:daily_calls", int(config.env("LLM_CALLS_PER_DAY", "1000")), 86400)
    await resources.reserve(
        "llm:daily_tokens",
        int(config.env("LLM_TOKEN_BUDGET_PER_DAY", "1000000")),
        86400,
        reserve_tokens,
    )
    async with state.llm_slots:
        async with AsyncOpenAI(
            base_url=provider.base_url, api_key=provider.api_key, max_retries=0
        ) as client:
            completion = await client.chat.completions.create(
                model=provider.model, messages=messages, timeout=15, max_tokens=1000
            )
    usage = completion.usage
    return LLMResult(
        text=completion.choices[0].message.content or "",
        provider=provider.name,
        model=provider.model,
        prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
        completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        total_tokens=getattr(usage, "total_tokens", None) if usage else None,
    )
