"""Cliente de documentos: respuesta validada y PDF autenticado/acotado."""

import logging
import math
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from app.core import config

logger = logging.getLogger("chatbot.document_service")
API_KEY_HEADER = "X-Internal-API-Key"
REQUEST_TIMEOUT = 30.0
MAX_PDF_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class QuoteResult:
    document_id: str
    quote_number: str
    pdf_url: str
    size_bytes: int
    status: str


def _allowed_url(url: str, base_url: str) -> bool:
    try:
        parsed, base = (urlsplit(url), urlsplit(base_url))
        return (
            parsed.scheme in {"http", "https"}
            and parsed.scheme == base.scheme
            and (parsed.hostname == base.hostname)
            and (parsed.port == base.port)
            and (parsed.hostname is not None)
            and (not parsed.username)
            and (not parsed.password)
            and (not parsed.fragment)
            and ("\\" not in url)
            and (".." not in unquote(parsed.path).split("/"))
        )
    except ValueError:
        return False


def _build_pdf_url(base_url: str, relative_url: str) -> str:
    if (
        not isinstance(relative_url, str)
        or not relative_url
        or ".." in unquote(urlsplit(relative_url).path).split("/")
    ):
        raise ValueError("URL PDF inválida")
    url = urljoin(base_url.rstrip("/") + "/", relative_url)
    if not _allowed_url(url, base_url):
        raise ValueError("Origen PDF no autorizado")
    return url


def _parse_quote_response(base_url: str, data: dict) -> QuoteResult | None:
    try:
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise ValueError
        pdf = next((f for f in data["files"] if isinstance(f, dict) and f.get("format") == "pdf"))
        size = pdf.get("size_bytes", 0)
        if isinstance(size, bool) or not isinstance(size, int) or (not 0 <= size <= MAX_PDF_BYTES):
            raise ValueError
        fields = [data.get(k) for k in ("document_id", "quote_number", "status")]
        if any((not isinstance(v, str) or not v or len(v) > 200 for v in fields)):
            raise ValueError
        return QuoteResult(
            fields[0], fields[1], _build_pdf_url(base_url, pdf.get("url")), size, fields[2]
        )
    except (ValueError, TypeError, StopIteration):
        logger.warning("Respuesta del servicio de documentos inválida")
        return None


async def create_quote(
    request_id: str, tenant_id: str, customer: dict, items: list[dict], currency: str = "DOP"
) -> QuoteResult | None:
    base_url, api_key = (config.env("DOCUMENT_SERVICE_URL", ""), config.env("INTERNAL_API_KEY", ""))
    if not base_url or not api_key or (not _allowed_url(base_url, base_url)):
        logger.warning("Configuración del servicio de documentos incompleta")
        return None
    try:
        wire_items = []
        for item in items:
            quantity, price = (float(item["quantity"]), float(item["unit_price"]))
            if not math.isfinite(quantity) or not math.isfinite(price):
                raise ValueError("Importe no finito")
            wire_items.append(
                {"description": item["description"], "quantity": quantity, "unit_price": price}
            )
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            resp = await client.post(
                base_url.rstrip("/") + "/quotes",
                json={
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "customer": customer,
                    "items": wire_items,
                    "currency": currency,
                },
                headers={API_KEY_HEADER: api_key},
            )
        if resp.status_code not in (200, 201):
            logger.warning("Servicio de documentos rechazó la solicitud (%s)", resp.status_code)
            return None
        return _parse_quote_response(base_url, resp.json())
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Fallo del servicio de documentos: %s", type(exc).__name__)
        return None


async def fetch_quote_pdf(pdf_url: str) -> bytes | None:
    base_url = config.env("DOCUMENT_SERVICE_URL", "")
    if not base_url or not _allowed_url(pdf_url, base_url):
        logger.warning("Origen de descarga no autorizado")
        return None
    headers = {API_KEY_HEADER: config.env("INTERNAL_API_KEY", "")}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            async with client.stream("GET", pdf_url, headers=headers) as resp:
                if (
                    resp.status_code != 200
                    or resp.headers.get("content-type", "").split(";")[0].strip()
                    != "application/pdf"
                ):
                    return None
                length = resp.headers.get("content-length")
                if length and (int(length) < 0 or int(length) > MAX_PDF_BYTES):
                    return None
                content = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    content.extend(chunk)
                    if len(content) > MAX_PDF_BYTES:
                        return None
                return bytes(content) if content.startswith(b"%PDF-") else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Fallo de descarga: %s", type(exc).__name__)
        return None
