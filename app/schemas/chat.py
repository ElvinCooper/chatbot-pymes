"""Contratos acotados de chat_schema."""

from typing import Annotated

from pydantic import Field

import app.schemas.base as schema_base


class ChatRequest(schema_base.Contract):
    message: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    conversation_id: Annotated[str, Field(strict=True, max_length=100)] | None = None
