"""Contratos acotados de quote_schema."""

import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator

import app.schemas.base as schema_base

Text = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Amount = Annotated[Decimal, Field(ge=0, le=1000000, allow_inf_nan=False, decimal_places=2)]
Quantity = Annotated[Decimal, Field(gt=0, le=10000, allow_inf_nan=False, decimal_places=4)]


class Customer(schema_base.Contract):
    name: Text = "Cliente"
    phone: Annotated[str, Field(strict=True, max_length=40)] = ""


class QuoteItem(schema_base.Contract):
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


class QuoteDraft(schema_base.Contract):
    intent: Literal["quote"]
    customer: Customer = Field(default_factory=Customer)
    currency: Literal["DOP", "USD", "EUR"] = "DOP"
    event_date: Annotated[str, Field(strict=True, max_length=10)] = ""
    party_size: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0
    items: Annotated[list[QuoteItem], Field(max_length=50)] = Field(default_factory=list)

    @field_validator("event_date")
    @classmethod
    def valid_date(cls, value):
        return datetime.date.fromisoformat(value).isoformat() if value else ""


class QuoteRequest(schema_base.Contract):
    request_id: Annotated[
        str, Field(strict=True, min_length=1, max_length=100, pattern="^[A-Za-z0-9_-]+$")
    ]
    tenant_id: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    customer: Customer
    items: Annotated[list[QuoteItem], Field(min_length=1, max_length=50)]
    currency: Literal["DOP", "USD", "EUR"] = "DOP"
