"""
Adapter del servicio de correos (email-service) para el chatbot.

Envía correos transaccionales al endpoint POST /api/v1/emails/send del
microservicio email-service, que a su vez los entrega con Resend.

Igual que document_service.py, es una capa de transporte: no decide cuándo
enviar un correo, solo cómo hablar con el servicio. Hoy ningún flujo lo llama;
queda disponible para cuando se conecte (p. ej. mandar el PDF de una cotización
al cliente por correo además de por Telegram/WhatsApp).

El remitente NO lo elige este módulo: el email-service lo toma siempre de su
propio EMAIL_FROM. Si se mandara un "from", el servicio responde 422
INVALID_REQUEST en lugar de aceptarlo en silencio.

Idempotencia: el email-service nunca genera la clave por su cuenta, a
propósito. Si no se manda Idempotency-Key, un reintento por timeout vuelve a
enviar el correo y el cliente lo recibe duplicado. Usa build_idempotency_key()
con un identificador estable del envío (request_id, quote_number...).

Configuración vía variables de entorno (ver .env.example):
  EMAIL_SERVICE_URL     URL base, p. ej. http://127.0.0.1:8003
  EMAIL_SERVICE_API_KEY clave compartida (cabecera X-API-Key)

Ojo: esta clave es DISTINTA de INTERNAL_API_KEY, que es la de document-service.
Cada microservicio tiene la suya. La clave de Resend nunca sale del
email-service: el chatbot no la necesita ni debe conocerla.
"""

import base64
import logging
from dataclasses import dataclass

import httpx

from app.core import config

logger = logging.getLogger("chatbot.email_service")
API_KEY_HEADER = "X-API-Key"
REQUEST_TIMEOUT = 30.0
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class EmailResult:
    """Resultado de un correo aceptado por el email-service."""

    email_id: str
    tenant_id: str


def build_idempotency_key(scope: str, entity_id: str) -> str:
    """Construye la cabecera Idempotency-Key de un envío.

    Formato <tipo-evento>/<id-entidad>, el que recomienda Resend. La clave debe
    ser estable para el mismo envío lógico (así un reintento no duplica) y
    distinta para cada envío. Límite del servicio: 256 caracteres.
    """
    return f"{scope}/{entity_id}"


def encode_attachment(filename: str, content: bytes, content_type: str) -> dict:
    """Prepara un adjunto en el formato que espera el email-service (Base64).

    El contenido viaja en memoria: no se escribe ningún fichero temporal.
    """
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Adjunto demasiado grande")
    return {
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_type": content_type,
    }


async def send_email(
    tenant_id: str,
    to: list[str] | str,
    subject: str,
    text: str | None = None,
    html: str | None = None,
    attachments: list[dict] | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
    reply_to: list[str] | str | None = None,
) -> EmailResult | None:
    """Envía un correo a través del email-service y devuelve el resultado.

    `to` acepta un texto suelto o una lista, igual que el servicio. El remitente
    no se incluye nunca en el payload: lo pone el email-service desde EMAIL_FROM.

    Ante fallos de configuración, de red o de la API devuelve None (loggeado),
    con la misma convención que create_quote de document_service.py.
    """
    base_url = config.env("EMAIL_SERVICE_URL") or ""
    api_key = config.env("EMAIL_SERVICE_API_KEY") or ""
    if not base_url or not api_key:
        logger.warning(
            "Faltan EMAIL_SERVICE_URL/EMAIL_SERVICE_API_KEY; no se puede enviar el correo."
        )
        return None
    if not idempotency_key or len(idempotency_key) > 256:
        logger.warning("Idempotency-Key obligatoria e inválida/ausente")
        return None
    if attachments:
        try:
            sizes = [len(base64.b64decode(a["content_base64"], validate=True)) for a in attachments]
            if (
                len(sizes) > MAX_ATTACHMENTS
                or max(sizes) > MAX_ATTACHMENT_BYTES
                or sum(sizes) > MAX_TOTAL_ATTACHMENT_BYTES
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            logger.warning("Adjuntos inválidos o demasiado grandes")
            return None
    payload: dict = {
        "tenant_id": tenant_id,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
    }
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html
    if attachments:
        payload["attachments"] = attachments
    if reply_to:
        payload["reply_to"] = [reply_to] if isinstance(reply_to, str) else list(reply_to)
    if metadata:
        payload["metadata"] = metadata
    headers = {API_KEY_HEADER: api_key, "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/v1/emails/send", json=payload, headers=headers
            )
    except httpx.HTTPError as exc:
        logger.warning("Error de red al llamar al email-service: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning(
            "Error al enviar el correo en email-service (%s): %s",
            resp.status_code,
            "respuesta omitida",
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning(
            "Respuesta no JSON del email-service (%s): %s", resp.status_code, "respuesta omitida"
        )
        return None
    return _parse_email_response(data)


def _parse_email_response(data: dict) -> EmailResult | None:
    """Convierte la respuesta de POST /api/v1/emails/send en EmailResult.

    El 200 ya implica envío aceptado; aun así se comprueba "success" porque el
    contrato de error usa la misma forma de envelope.
    """
    if not isinstance(data, dict):
        return None
    if data.get("success") is not True:
        error = data.get("error") or {}
        if not isinstance(error, dict):
            error = {}
        logger.warning(
            "El email-service respondió success=false (%s): %s",
            error.get("code", "sin código"),
            "mensaje omitido",
        )
        return None
    email_id = data.get("email_id")
    if not isinstance(email_id, str) or not email_id or len(email_id) > 200:
        logger.warning("Respuesta del email-service sin email_id")
        return None
    return EmailResult(email_id=email_id, tenant_id=data.get("tenant_id", ""))
