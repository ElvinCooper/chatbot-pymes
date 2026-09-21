"""
Adapter del servicio de documentos (document-service) para el chatbot.

Envía cotizaciones al endpoint POST /quotes del microservicio document-service
y parsea la respuesta para exponer el PDF generado. La idempotencia la
resuelve el microservicio: reenviar el mismo request_id + tenant_id devuelve
el PDF ya generado (HTTP 200) en lugar de duplicarlo (HTTP 201).

Al igual que telegram_adapter.py y whatsapp_adapter.py, es una capa de
transporte: el orquestado (cuándo generar una cotización) lo decide main.py.

Configuración vía variables de entorno (ver .env.example):
  DOCUMENT_SERVICE_URL  URL base, p. ej. http://document-service:8000
  INTERNAL_API_KEY      clave interna compartida (cabecera X-Internal-API-Key)
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger("chatbot.document_service")

API_KEY_HEADER = "X-Internal-API-Key"
REQUEST_TIMEOUT = 30.0


@dataclass(frozen=True)
class QuoteResult:
    """Resultado de una cotización generada por el document-service."""

    document_id: str
    quote_number: str
    pdf_url: str
    size_bytes: int
    status: str


def _build_pdf_url(base_url: str, relative_url: str) -> str:
    """Convierte el URL relativo devuelto por el servicio en URL absoluto."""
    return f"{base_url.rstrip('/')}/{relative_url.lstrip('/')}"


def _parse_quote_response(base_url: str, data: dict) -> QuoteResult | None:
    """Convierte la respuesta JSON de POST /quotes en QuoteResult.

    El servicio puede devolver varios formatos en "files"; se busca el PDF.
    """
    files = data.get("files") or []
    pdf = next((f for f in files if f.get("format") == "pdf"), None)
    if not pdf:
        logger.warning("Respuesta del document-service sin archivo PDF: %s", data)
        return None
    return QuoteResult(
        document_id=data.get("document_id", ""),
        quote_number=data.get("quote_number", ""),
        pdf_url=_build_pdf_url(base_url, pdf.get("url", "")),
        size_bytes=int(pdf.get("size_bytes", 0)),
        status=data.get("status", ""),
    )


async def create_quote(
    request_id: str,
    tenant_id: str,
    customer: dict,
    items: list[dict],
    currency: str = "DOP",
) -> QuoteResult | None:
    """Genera una cotización en el document-service y devuelve el resultado.

    Ante fallos de configuración, de red o de la API devuelve None (loggeado),
    siguiendo la misma convención que send_message de whatsapp_adapter.py.
    """
    base_url = os.getenv("DOCUMENT_SERVICE_URL") or ""
    api_key = os.getenv("INTERNAL_API_KEY") or ""
    if not base_url or not api_key:
        logger.warning(
            "Faltan DOCUMENT_SERVICE_URL/INTERNAL_API_KEY; no se puede generar la cotización."
        )
        return None

    payload: dict = {
        "request_id": request_id,
        "tenant_id": tenant_id,
        "customer": customer,
        "items": items,
        "currency": currency,
    }
    headers = {
        API_KEY_HEADER: api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/quotes", json=payload, headers=headers
            )
    except httpx.HTTPError as exc:
        logger.warning("Error de red al llamar al document-service: %s", exc)
        return None

    if resp.status_code not in (200, 201):
        logger.warning(
            "Error al generar cotización en document-service (%s): %s",
            resp.status_code, resp.text,
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.warning("Respuesta no JSON del document-service (%s): %s", resp.status_code, resp.text)
        return None

    return _parse_quote_response(base_url, data)