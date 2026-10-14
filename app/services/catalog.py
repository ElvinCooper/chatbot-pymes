"""Precios autoritativos; ningún precio aportado por el LLM es confiable."""

import json
import unicodedata
from decimal import Decimal

import app.schemas.quotes as quote_schema
from app.core import config


def normalize(value: str) -> str:
    return " ".join(
        "".join(
            (
                c
                for c in unicodedata.normalize("NFKD", value.lower())
                if not unicodedata.combining(c)
            )
        ).split()
    )


def products() -> dict:
    path = config.resolve_path(config.env("QUOTE_CATALOG_PATH"), config.QUOTE_CATALOG_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def prompt_catalog() -> str:
    return json.dumps(
        {
            key: {
                "description": item["description"],
                "currency": item["currency"],
                "requires_review": item.get("requires_review", False),
            }
            for key, item in products().items()
        },
        ensure_ascii=False,
    )


def price_draft(draft: dict) -> dict:
    """Coincidencia exacta; productos personalizados requieren revisión humana."""
    parsed = quote_schema.QuoteDraft.model_validate(draft)
    catalog = products()
    output = parsed.model_dump(mode="json")
    for item in output["items"]:
        match = None
        if item["product_id"]:
            match = catalog.get(item["product_id"])
        else:
            description = normalize(item["description"])
            for product in catalog.values():
                aliases = [product["description"], *product.get("aliases", [])]
                if description in {normalize(alias) for alias in aliases}:
                    match = product
                    break
        if not match or match.get("requires_review") or match["currency"] != parsed.currency:
            raise ValueError("El negocio debe confirmar el producto/precio personalizado")
        amount = Decimal(match["unit_price"])
        if not amount.is_finite() or amount < 0 or amount > 1000000:
            raise ValueError("Precio de catálogo inválido")
        item["description"] = match["description"]
        item["unit_price"] = str(amount.quantize(Decimal("0.01")))
        item.pop("product_id", None)
    return output
