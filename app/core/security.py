"""Autenticación, límites HTTP y logs sin secretos ni cuerpos de clientes."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from starlette.responses import JSONResponse

from app.core import config


@dataclass(frozen=True)
class Identity:
    client_id: str
    admin: bool = False


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def authenticate(authorization: Annotated[str | None, Header()] = None) -> Identity:
    """Cada cliente obtiene una clave e identidad distinta; sin config se cierra."""
    token = (authorization or "").removeprefix("Bearer ")
    if not authorization or not authorization.startswith("Bearer ") or len(token) > 512:
        raise HTTPException(401, "Autenticación requerida", headers={"WWW-Authenticate": "Bearer"})
    admin = config.env("ADMIN_API_KEY", "")
    if len(admin) >= 32 and _equal(token, admin):
        return Identity("admin", True)
    try:
        clients = json.loads(config.env("API_CLIENT_KEYS", "{}"))
        if not isinstance(clients, dict):
            raise ValueError
        for client_id, key in clients.items():
            if (
                re.fullmatch("[A-Za-z0-9_-]{1,64}", client_id)
                and isinstance(key, str)
                and (len(key) >= 32)
                and _equal(token, key)
            ):
                return Identity(client_id)
    except (ValueError, TypeError):
        raise HTTPException(503, "Configuración de autenticación inválida") from None
    raise HTTPException(401, "Credencial inválida", headers={"WWW-Authenticate": "Bearer"})


IdentityDep = Annotated[Identity, Depends(authenticate)]


def require_admin(identity: IdentityDep) -> Identity:
    if not identity.admin:
        raise HTTPException(403, "Permiso administrativo requerido")
    return identity


def api_conversation(identity: Identity, supplied: str | None) -> str | None:
    if supplied is None:
        return None
    if not re.fullmatch("[A-Za-z0-9_-]{1,100}", supplied):
        raise HTTPException(422, "Identificador de sesión inválido")
    digest = hashlib.sha256(f"{identity.client_id}:{supplied}".encode()).hexdigest()
    return f"api:{digest}"


class BodyLimitMiddleware:
    """Cuenta bytes reales, incluso sin Content-Length, antes del parser multipart."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        upload = scope["path"] == "/documents/upload"
        limit = 6 * 1024 * 1024 if upload else 256 * 1024
        body = bytearray()
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            try:
                async with asyncio.timeout(60):
                    while True:
                        event = await asyncio.wait_for(receive(), timeout=10)
                        if event["type"] == "http.disconnect":
                            return
                        body.extend(event.get("body", b""))
                        if len(body) > limit:
                            response = JSONResponse(
                                {"detail": "Cuerpo demasiado grande"}, status_code=413
                            )
                            return await response(scope, receive, send)
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                response = JSONResponse({"detail": "Tiempo de recepción agotado"}, status_code=408)
                return await response(scope, receive, send)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class RedactFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub("/bot[^/\\s]+/", "/bot[REDACTADO]/", message)
        message = re.sub(
            "([?&](?:hub\\.verify_token|access_token|api_key|token)=)[^&\\s]+",
            "\\1[REDACTADO]",
            message,
        )
        for name, value in os.environ.items():
            if (
                any((part in name for part in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")))
                and len(value) >= 8
            ):
                message = message.replace(value, "[REDACTADO]")
        record.msg, record.args = (message, ())
        if record.exc_info:
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging():
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True


def validate_meta(raw: bytes, signature: str | None, secret: str):
    if not secret:
        raise HTTPException(503, "Canal no configurado de forma segura")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not _equal(signature, expected):
        raise HTTPException(401, "Firma de webhook inválida")


def parse_webhook(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("entry", []), list):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Body inválido") from None


def verify_webhook_secret(x_telegram_bot_api_secret_token: str | None = None) -> None:
    """Valida el origen del webhook. Si hay un secret configurado, exige que la
    cabecera coincida; si no, responde 401 (evita usar /webhooks/telegram como
    proxy hacia /chat)."""
    if not config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Canal no configurado de forma segura")
    if not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode(), config.TELEGRAM_WEBHOOK_SECRET.encode()
    ):
        raise HTTPException(status_code=401, detail="Secret de webhook inválido")
