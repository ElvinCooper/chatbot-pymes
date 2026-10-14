"""Tests de los eventos de canal de WhatsApp (Fase 2).

Verifican que:
  - un mensaje entrante válido crea un evento `message_received` con
    wamid/contact_id/phone_number_id/conversation_id/tenant_id y el timestamp
    que reporta Meta;
  - un wamid duplicado NO crea un segundo `message_received`;
  - el envío exitoso crea `message_sent` con el wamid de Meta (o NULL si Meta
    no lo devuelve) y no se registra nada ante un error de Meta;
  - un fallo de SQLite nunca rompe ni la recepción ni el envío (best-effort);
  - `send_document` conserva su comportamiento y registra `message_sent`.

Usan un SQLite temporal y mockean solo los clientes HTTP de Meta (stdlib
unittest, sin dependencias adicionales):
    .venv/bin/python -m unittest tests/test_whatsapp_metrics.py -v
"""

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import httpx
from fastapi import BackgroundTasks
from starlette.requests import Request

from app import main
from app.api import webhooks
from app.core import config
from app.integrations.channels import whatsapp
from app.persistence import database, metrics


def _whatsapp_payload(
    wamid: str = "wamid.CE4",
    ts: int | None = None,
    contact: str = "5491100000000",
    phone_number_id: str = "123456",
) -> dict:
    """Recrea la forma de un update de mensajes de WhatsApp de Meta."""
    if ts is None:
        ts = int(time.time()) - 30
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "3123",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551234",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Cliente"}, "wa_id": contact}],
                            "messages": [
                                {
                                    "from": contact,
                                    "id": wamid,
                                    "timestamp": ts,
                                    "type": "text",
                                    "text": {"body": "Hola, necesito una cotización"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


class WhatsappMetricsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "app.persistence.metrics.sqlite3")
        metrics.METRICS_DB_PATH = self._db_path
        metrics._connect().close()
        self._inbox_path = database.DB_PATH
        database.DB_PATH = os.path.join(self._tmpdir.name, "inbox.sqlite3")
        self._verify_token = config.WHATSAPP_VERIFY_TOKEN
        self._app_secret = config.WHATSAPP_APP_SECRET
        config.WHATSAPP_VERIFY_TOKEN = "verify-test"
        config.WHATSAPP_APP_SECRET = "secret-test"
        self._env = {
            "WHATSAPP_ACCESS_TOKEN": os.environ.get("WHATSAPP_ACCESS_TOKEN"),
            "WHATSAPP_PHONE_NUMBER_ID": os.environ.get("WHATSAPP_PHONE_NUMBER_ID"),
        }
        os.environ["WHATSAPP_ACCESS_TOKEN"] = "test-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "123456"

    def tearDown(self):
        database.DB_PATH = self._inbox_path
        config.WHATSAPP_VERIFY_TOKEN = self._verify_token
        config.WHATSAPP_APP_SECRET = self._app_secret
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()

    def _query(self, sql: str, params=()):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _request(self, raw_body: bytes) -> Request:
        """Construye un Request de Starlette para el endpoint del webhook."""

        async def receive():
            yield_bodies = {"sent": False, "body": raw_body}

            async def _read():
                if not yield_bodies["sent"]:
                    yield_bodies["sent"] = True
                    return {"type": "http.request", "body": raw_body, "more_body": False}
                return {"type": "http.disconnect"}

            return await _read()

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/webhooks/whatsapp",
            "raw_path": b"/webhooks/whatsapp",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"application/json"),
                (b"host", b"testserver"),
                (
                    b"x-hub-signature-256",
                    (
                        "sha256="
                        + hmac.new(
                            config.WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256
                        ).hexdigest()
                    ).encode(),
                ),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": main.app,
        }
        return Request(scope, receive)

    async def _fire_webhook(self, raw_body: bytes) -> dict:
        background_tasks = BackgroundTasks()
        return await webhooks.whatsapp_webhook(self._request(raw_body), background_tasks)

    def _hit_meta(self, response: httpx.Response):
        """Mockea el cliente HTTP de la Graph API para un único POST."""
        client = mock.AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        patcher = mock.patch(
            "app.integrations.channels.whatsapp.httpx.AsyncClient", return_value=client
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    async def test_mensaje_inbound_valido_registra_message_received(self):
        ts = int(time.time()) - 30
        raw = json.dumps(_whatsapp_payload(ts=ts)).encode()
        result = await self._fire_webhook(raw)
        self.assertEqual(result, {"status": "ok"})
        rows = self._query(
            (
                "SELECT event_type, direction, channel, wamid, contact_id, phone_number_id, "
                "conversation_id, tenant_id, payload_size, event_time, metadata FROM usage_events"
            )
        )
        self.assertEqual(len(rows), 1)
        (
            event_type,
            direction,
            channel,
            wamid,
            contact_id,
            phone_number_id,
            conversation_id,
            tenant_id,
            payload_size,
            event_time,
            metadata,
        ) = rows[0]
        self.assertEqual(event_type, "message_received")
        self.assertEqual(direction, "inbound")
        self.assertEqual(channel, "whatsapp")
        self.assertEqual(wamid, "wamid.CE4")
        self.assertEqual(contact_id, "5491100000000")
        self.assertEqual(phone_number_id, "123456")
        self.assertEqual(conversation_id, "whatsapp:5491100000000")
        self.assertEqual(tenant_id, "chatbot")
        self.assertEqual(payload_size, len(raw))
        self.assertEqual(
            event_time, datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        )
        self.assertEqual(json.loads(metadata), {"message_type": "text"})

    async def test_wamid_duplicado_no_crea_segundo_evento(self):
        raw = json.dumps(_whatsapp_payload()).encode()
        await self._fire_webhook(raw)
        await self._fire_webhook(raw)
        rows = self._query("SELECT event_type FROM usage_events")
        self.assertEqual(rows, [("message_received",)])

    async def test_send_message_ok_registra_message_sent_con_wamid(self):
        self._hit_meta(
            httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": "5491100000000"}],
                    "messages": [{"id": "wamid.ABC123"}],
                },
            )
        )
        await whatsapp.send_message("5491100000000", "Hola")
        rows = self._query(
            "SELECT event_type, direction, channel, wamid, contact_id, conversation_id, tenant_id, metadata FROM usage_events"
        )
        self.assertEqual(len(rows), 1)
        event_type, direction, channel, wamid, contact_id, conv, tenant, metadata = rows[0]
        self.assertEqual(event_type, "message_sent")
        self.assertEqual(direction, "outbound")
        self.assertEqual(channel, "whatsapp")
        self.assertEqual(wamid, "wamid.ABC123")
        self.assertEqual(contact_id, "5491100000000")
        self.assertEqual(conv, "whatsapp:5491100000000")
        self.assertEqual(tenant, "chatbot")
        self.assertEqual(json.loads(metadata), {"message_type": "text"})

    async def test_send_message_sin_wamid_registra_null(self):
        self._hit_meta(
            httpx.Response(
                200, json={"messaging_product": "whatsapp", "contacts": [{"wa_id": "x"}]}
            )
        )
        await whatsapp.send_message("5491100000000", "Hola")
        rows = self._query("SELECT wamid, event_type FROM usage_events")
        self.assertEqual(rows, [(None, "message_sent")])

    async def test_error_de_meta_no_registra_message_sent(self):
        self._hit_meta(httpx.Response(403, json={"error": {"message": "fuera de ventana"}}))
        await whatsapp.send_message("5491100000000", "Hola")
        self.assertEqual(self._query("SELECT 1 FROM usage_events"), [])

    async def test_fallo_de_sqlite_no_rompe_recepcion_ni_envio(self):
        with mock.patch(
            "app.persistence.metrics._connect", side_effect=RuntimeError("disco lleno")
        ):
            result = await self._fire_webhook(json.dumps(_whatsapp_payload()).encode())
            self.assertEqual(result, {"status": "ok"})
            self._hit_meta(httpx.Response(200, json={"messages": [{"id": "wamid.X"}]}))
            await whatsapp.send_message("5491100000000", "Hola")
        self.assertEqual(self._query("SELECT 1 FROM usage_events"), [])

    async def test_send_document_ok_registra_message_sent(self):

        def _post(url: str, **kwargs):
            if url.endswith("/media"):
                return httpx.Response(200, json={"id": "media_999"})
            return httpx.Response(200, json={"messages": [{"id": "wamid.DOC"}]})

        client = mock.AsyncMock()
        client.post.side_effect = _post
        client.__aenter__.return_value = client
        with mock.patch(
            "app.integrations.channels.whatsapp.httpx.AsyncClient", return_value=client
        ):
            await whatsapp.send_document(
                "5491100000000", b"%PDF-1.4 fake", "cotizacion.pdf", "Cotización"
            )
        rows = self._query("SELECT event_type, wamid, contact_id, metadata FROM usage_events")
        self.assertEqual(len(rows), 1)
        event_type, wamid, contact_id, metadata = rows[0]
        self.assertEqual(event_type, "message_sent")
        self.assertEqual(wamid, "wamid.DOC")
        self.assertEqual(contact_id, "5491100000000")
        self.assertEqual(json.loads(metadata), {"message_type": "document"})
        self.assertEqual(client.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
