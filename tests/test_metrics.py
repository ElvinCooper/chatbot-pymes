"""Tests mínimos de la capa de métricas (Fase 0).

Usan un SQLite temporal (stdlib unittest, sin dependencias adicionales):
    .venv/bin/python -m unittest tests/test_metrics.py -v

Se verifica que la BD y la tabla se crean solas, que el insert persiste
provider/model/usage/tenant/event_type, que usage ausente => NULL y que un
fallo de SQLite nunca rompe el flujo (best-effort).
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import metrics


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "metrics.sqlite3")
        metrics.METRICS_DB_PATH = self._db_path

    def tearDown(self):
        self._tmpdir.cleanup()

    def _query(self, sql: str, params=()):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_db_y_tabla_autocreadas(self):
        metrics.record_usage_event(tenant_id="chatbot", event_type="chat_reply")
        self.assertTrue(os.path.exists(self._db_path))
        rows = self._query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_events'"
        )
        self.assertEqual(rows, [("usage_events",)])

    def test_estructura_de_columnas(self):
        metrics.record_usage_event(tenant_id="chatbot", event_type="chat_reply")
        cols = [r[1] for r in self._query("PRAGMA table_info(usage_events)")]
        for expected in (
            "id", "event_time", "tenant_id", "channel", "direction",
            "event_type", "conversation_id", "provider", "model",
            "prompt_tokens", "completion_tokens", "total_tokens",
            "metadata", "created_at",
        ):
            self.assertIn(expected, cols)

    def test_insert_y_persistencia(self):
        metrics.record_usage_event(
            tenant_id="chatbot",
            event_type="chat_reply",
            channel="whatsapp",
            conversation_id="whatsapp:5491100000000",
            provider="groq",
            model="openai/gpt-oss-120b",
            prompt_tokens=320,
            completion_tokens=45,
            total_tokens=365,
            metadata={"used_context": True},
        )
        rows = self._query(
            "SELECT tenant_id, channel, event_type, conversation_id, provider, "
            "model, prompt_tokens, completion_tokens, total_tokens, metadata "
            "FROM usage_events"
        )
        self.assertEqual(len(rows), 1)
        (
            tenant_id, channel, event_type, conv, provider, model,
            prompt, completion, total, metadata,
        ) = rows[0]
        self.assertEqual(tenant_id, "chatbot")
        self.assertEqual(channel, "whatsapp")
        self.assertEqual(event_type, "chat_reply")
        self.assertEqual(conv, "whatsapp:5491100000000")
        self.assertEqual(provider, "groq")
        self.assertEqual(model, "openai/gpt-oss-120b")
        self.assertEqual((prompt, completion, total), (320, 45, 365))
        self.assertEqual(json.loads(metadata), {"used_context": True})

    def test_usage_ausente_queda_null(self):
        # El proveedor no reportó usage: los tokens NO se estiman, quedan NULL.
        metrics.record_usage_event(
            tenant_id="chatbot",
            event_type="quote_extraction",
            provider="gemini",
            model="gemini-3.6-flash",
        )
        prompt, completion, total = self._query(
            "SELECT prompt_tokens, completion_tokens, total_tokens FROM usage_events"
        )[0]
        self.assertIsNone(prompt)
        self.assertIsNone(completion)
        self.assertIsNone(total)

    def test_fallo_de_sqlite_no_rompe_el_flujo(self):
        # Best-effort: si SQLite falla se loguea y el evento se descarta en
        # silencio; nunca se propaga la excepción al flujo del chatbot.
        with mock.patch(
            "metrics.sqlite3.connect", side_effect=RuntimeError("disco lleno")
        ):
            metrics.record_usage_event(tenant_id="chatbot", event_type="chat_reply")
        # Sin fallo de SQLite, el evento sí se inserta.
        metrics.record_usage_event(tenant_id="chatbot", event_type="chat_reply")
        self.assertEqual(
            len(self._query("SELECT 1 FROM usage_events")), 1,
        )


if __name__ == "__main__":
    unittest.main()