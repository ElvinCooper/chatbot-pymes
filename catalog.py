"""Precios autoritativos; ningún precio aportado por el LLM es confiable."""

import json
import os
from pathlib import Path
import unicodedata
from decimal import Decimal

from schemas import QuoteDraft


def normalize(value: str) -> str:
    return " ".join(
        "".join(
            c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c)
        ).split()
    )


def products() -> dict:
    path = Path(
        os.getenv("QUOTE_CATALOG_PATH", str(Path(__file__).parent / "data" / "quote_catalog.json"))
    )
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
    parsed = QuoteDraft.model_validate(draft)
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
        if not amount.is_finite() or amount < 0 or amount > 1_000_000:
            raise ValueError("Precio de catálogo inválido")
        # Descripción también autoritativa: el documento no hereda instrucciones.
        item["description"] = match["description"]
        item["unit_price"] = str(amount.quantize(Decimal("0.01")))
        item.pop("product_id", None)
    return output
