"""Contratos acotados para entradas HTTP y extracción no confiable del LLM."""

from decimal import Decimal
from typing import Annotated, Literal
import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

Text = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Amount = Annotated[Decimal, Field(ge=0, le=1_000_000, allow_inf_nan=False, decimal_places=2)]
Quantity = Annotated[Decimal, Field(gt=0, le=10_000, allow_inf_nan=False, decimal_places=4)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChatRequest(Contract):
    message: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    conversation_id: Annotated[str, Field(strict=True, max_length=100)] | None = None


class Customer(Contract):
    name: Text = "Cliente"
    phone: Annotated[str, Field(strict=True, max_length=40)] = ""


class QuoteItem(Contract):
    description: Annotated[str, Field(strict=True, min_length=1, max_length=300)]
    product_id: Annotated[str, Field(strict=True, max_length=100)] = ""
    quantity: Quantity = Decimal("1")
    unit_price: Amount = Decimal("0")

    @field_validator("quantity", "unit_price", mode="before")
    @classmethod
    def reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("Un booleano no es un importe ni cantidad")
        return value


class QuoteDraft(Contract):
    intent: Literal["quote"]
    customer: Customer = Field(default_factory=Customer)
    currency: Literal["DOP", "USD", "EUR"] = "DOP"
    event_date: Annotated[str, Field(strict=True, max_length=10)] = ""
    party_size: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 0
    items: Annotated[list[QuoteItem], Field(max_length=50)] = Field(default_factory=list)

    @field_validator("event_date")
    @classmethod
    def valid_date(cls, value):
        return datetime.date.fromisoformat(value).isoformat() if value else ""


class QuoteRequest(Contract):
    request_id: Annotated[
        str, Field(strict=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    tenant_id: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    customer: Customer
    items: Annotated[list[QuoteItem], Field(min_length=1, max_length=50)]
    currency: Literal["DOP", "USD", "EUR"] = "DOP"
