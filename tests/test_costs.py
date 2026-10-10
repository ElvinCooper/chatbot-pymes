"""Tests de la capa de costes (Fase 3).

SQLite temporal, stdlib unittest (sin dependencias adicionales):
    .venv/bin/python -m unittest tests/test_costs.py -v

Cubren: la matemática de coste IA, la idempotencia del seed de tarifas, el
UPSERT idempotente de estados de entrega, la selección de tarifa vigente y el
informe (tokens NULL, tarifa desconocida, tier gratuito de servicio, tenants sin
ingreso => sin margen).
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import costs
import metrics


class CostsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "metrics.sqlite3")
        metrics.METRICS_DB_PATH = self._db_path
        # Fuerza la creación del esquema (uso_events + tablas de Fase 3).
        metrics.record_usage_event(tenant_id="_init", event_type="chat_reply")
        self._clear_usage()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _clear_usage(self):
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM usage_events")
            conn.commit()
        finally:
            conn.close()

    def _query(self, sql, params=()):
        conn = sqlite3.connect(self._db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_llm_cost_matematica(self):
        # 1M de prompt a 0.15 + 1M de completion a 0.60 = 0.75
        self.assertAlmostEqual(
            costs.llm_cost_usd(1_000_000, 1_000_000, 0.15, 0.60), 0.75
        )
        self.assertAlmostEqual(
            costs.llm_cost_usd(998, 92, 0.15, 0.60),
            (998 * 0.15 + 92 * 0.60) / 1_000_000,
        )

    def test_llm_cost_tokens_ausentes_es_none(self):
        self.assertIsNone(costs.llm_cost_usd(None, 10, 0.15, 0.60))
        self.assertIsNone(costs.llm_cost_usd(10, None, 0.15, 0.60))

    def test_seed_idempotente(self):
        costs.seed_default_tariffs()
        first = self._query("SELECT COUNT(*) FROM tariffs")[0][0]
        self.assertEqual(first, len(costs.DEFAULT_TARIFFS))
        costs.seed_default_tariffs()
        self.assertEqual(self._query("SELECT COUNT(*) FROM tariffs")[0][0], first)

    def test_upsert_status_idempotente_y_max(self):
        metrics.upsert_message_status(
            wamid="w1", tenant_id="t1", status="sent", event_time="2026-10-05T00:00:00+00:00"
        )
        metrics.upsert_message_status(
            wamid="w1", tenant_id="t1", status="delivered", delivered=True,
            pricing_category="service", pricing_billable=True,
            event_time="2026-10-05T00:01:00+00:00",
        )
        # Un estado posterior sin pricing no debe borrar el pricing ya guardado.
        metrics.upsert_message_status(
            wamid="w1", tenant_id="t1", status="read", delivered=True,
            event_time="2026-10-05T00:02:00+00:00",
        )
        rows = self._query(
            "SELECT status, delivered, pricing_category, pricing_billable "
            "FROM whatsapp_message_status WHERE wamid='w1'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ("read", 1, "service", 1))

    def test_pick_llm_tariff_vigente(self):
        costs.seed_default_tariffs()
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            tariffs = [dict(r) for r in conn.execute("SELECT * FROM tariffs")]
        finally:
            conn.close()
        tariff, assumed = costs._pick_llm_tariff(
            tariffs, "groq", "openai/gpt-oss-120b", "2026-10-05"
        )
        self.assertIsNotNone(tariff)
        self.assertFalse(assumed)
        self.assertEqual(tariff["rate_input"], 0.15)
        # Fecha anterior a cualquier tarifa conocida => asumida.
        _, assumed_old = costs._pick_llm_tariff(
            tariffs, "groq", "openai/gpt-oss-120b", "2000-01-01"
        )
        self.assertTrue(assumed_old)

    def test_informe_completo(self):
        costs.seed_default_tariffs()

        # IA de "chatbot": un evento con coste y uno sin tokens.
        metrics.record_usage_event(
            tenant_id="chatbot", event_type="chat_reply",
            provider="groq", model="openai/gpt-oss-120b",
            prompt_tokens=1_000_000, completion_tokens=1_000_000,
            event_time="2026-10-05T10:00:00+00:00",
        )
        metrics.record_usage_event(
            tenant_id="chatbot", event_type="quote_extraction",
            provider="gemini", model="gemini-3.6-flash",
            event_time="2026-10-05T10:05:00+00:00",
        )
        # IA de otro tenant (sin ingreso).
        metrics.record_usage_event(
            tenant_id="otro", event_type="chat_reply",
            provider="groq", model="openai/gpt-oss-120b",
            prompt_tokens=1_000_000, completion_tokens=0,
            event_time="2026-10-06T10:00:00+00:00",
        )

        # WhatsApp: 1001 servicios entregados y facturables para el mismo número
        # => 1 cobrable tras el tier gratuito, a 0.0113.
        for i in range(1001):
            metrics.upsert_message_status(
                wamid=f"ws{i}", tenant_id="chatbot", phone_number_id="PN1",
                status="delivered", delivered=True,
                pricing_category="service", pricing_billable=True,
                event_time="2026-10-05T11:00:00+00:00",
            )
        # Un estado sin pricing => desconocido (excluido, no facturado).
        metrics.upsert_message_status(
            wamid="wu", tenant_id="chatbot", phone_number_id="PN1",
            status="delivered", delivered=True,
            event_time="2026-10-05T11:30:00+00:00",
        )
        # Ingreso del tenant "chatbot".
        metrics.set_monthly_revenue("chatbot", "2026-10", 10.0)

        report = costs.build_report("2026-10")
        t = report["tenants"]

        # chatbot: 0.75 IA + 0.0113 WA = 0.7613
        self.assertAlmostEqual(t["chatbot"]["ai_cost_usd"], 0.75, places=6)
        self.assertAlmostEqual(t["chatbot"]["wa_cost_usd"], 0.0113, places=6)
        self.assertEqual(t["chatbot"]["ai_events_unknown_tokens"], 1)
        self.assertEqual(t["chatbot"]["wa_delivered"], 1002)
        self.assertEqual(t["chatbot"]["wa_unknown_pricing"], 1)
        self.assertAlmostEqual(t["chatbot"]["revenue"], 10.0)
        self.assertAlmostEqual(
            t["chatbot"]["profit_usd"], 10.0 - (0.75 + 0.0113), places=6
        )
        self.assertIsNotNone(t["chatbot"]["margin_pct"])

        # "otro": 0.15 IA y sin ingreso => sin margen.
        self.assertAlmostEqual(t["otro"]["ai_cost_usd"], 0.15, places=6)
        self.assertIsNone(t["otro"]["revenue"])
        self.assertIsNone(t["otro"]["margin_pct"])

        # Totales: ingreso solo de chatbot; "otro" listado como sin ingreso.
        totals = report["totals"]
        self.assertAlmostEqual(totals["revenue"], 10.0)
        self.assertIn("otro", totals["tenants_without_revenue"])
        self.assertNotIn("chatbot", totals["tenants_without_revenue"])

        # El formateador no revienta.
        text = costs.format_report(report)
        self.assertIn("Informe de costes", text)
        self.assertIn("chatbot", text)

    def test_wa_tarifa_asumida_marca_coste_parcial(self):
        # Período anterior a cualquier tarifa WhatsApp conocida (la de utility
        # arranca 2026-07-01): el picker asume la más antigua y el informe debe
        # marcarlo (D3/D1/D2).
        costs.seed_default_tariffs()
        metrics.upsert_message_status(
            wamid="wu2", tenant_id="chatbot", phone_number_id="PN1",
            status="delivered", delivered=True,
            pricing_category="utility", pricing_billable=True,
            event_time="2026-05-10T11:00:00+00:00",
        )
        report = costs.build_report("2026-05")
        row = report["tenants"]["chatbot"]
        self.assertEqual(row["wa_assumed_rate"], 1)
        self.assertAlmostEqual(row["wa_cost_usd"], 0.0113, places=6)
        self.assertTrue(row["cost_partial"])
        self.assertTrue(report["totals"]["cost_partial"])

        text = costs.format_report(report)
        self.assertIn("PARCIAL", text)
        self.assertIn("tarifa asumida", text)
        self.assertIn("chatbot*", text)

    def test_llm_tarifa_rate_null_es_desconocida(self):
        # Tarifa LLM con rate NULL no debe costear 0 en silencio (D4).
        costs.seed_default_tariffs()
        metrics.upsert_tariff(
            kind="llm", provider="groq", model="otro-modelo",
            effective_from="2026-01-01", rate_input=None, rate_output=None,
        )
        metrics.record_usage_event(
            tenant_id="chatbot", event_type="chat_reply",
            provider="groq", model="otro-modelo",
            prompt_tokens=1_000_000, completion_tokens=0,
            event_time="2026-10-05T10:00:00+00:00",
        )
        report = costs.build_report("2026-10")
        row = report["tenants"]["chatbot"]
        self.assertEqual(row["ai_events_unknown_tariff"], 1)
        self.assertEqual(row["ai_cost_usd"], 0.0)
        self.assertTrue(row["cost_partial"])
        self.assertTrue(report["totals"]["cost_partial"])

    def test_wa_tarifa_rate_null_es_desconocida(self):
        # Tarifa WhatsApp con rate NULL no debe costear 0 en silencio (D4).
        costs.seed_default_tariffs()
        metrics.upsert_tariff(
            kind="whatsapp", provider="meta", market="rest_of_latam",
            category="custom_cat", effective_from="2026-09-01",
            rate=None, unit="per_message",
        )
        metrics.upsert_message_status(
            wamid="wc1", tenant_id="chatbot", phone_number_id="PN1",
            status="delivered", delivered=True,
            pricing_category="custom_cat", pricing_billable=True,
            event_time="2026-10-05T11:00:00+00:00",
        )
        report = costs.build_report("2026-10")
        row = report["tenants"]["chatbot"]
        self.assertEqual(row["wa_unknown_rate"], 1)
        self.assertEqual(row["wa_cost_usd"], 0.0)
        self.assertTrue(row["cost_partial"])
        self.assertTrue(report["totals"]["cost_partial"])

    def test_failed_precede_delivered(self):
        # Un wamid que llega 'delivered' y luego 'failed' NUNCA se factura como
        # entregado: failed es terminal (D6).
        costs.seed_default_tariffs()
        metrics.upsert_message_status(
            wamid="wf1", tenant_id="chatbot", phone_number_id="PN1",
            status="delivered", delivered=True,
            pricing_category="service", pricing_billable=True,
            event_time="2026-10-05T11:00:00+00:00",
        )
        metrics.upsert_message_status(
            wamid="wf1", tenant_id="chatbot", phone_number_id="PN1",
            status="failed", failed=True,
            event_time="2026-10-05T11:01:00+00:00",
        )
        report = costs.build_report("2026-10")
        row = report["tenants"]["chatbot"]
        self.assertEqual(row["wa_failed"], 1)
        self.assertEqual(row["wa_delivered"], 0)
        self.assertEqual(row["wa_cost_usd"], 0.0)
        self.assertFalse(row["cost_partial"])

    def test_informe_bd_ausente(self):
        os.remove(self._db_path)
        report = costs.build_report("2026-10")
        self.assertTrue(report["db_missing"])
        self.assertEqual(report["tenants"], {})
        self.assertIn("sin datos", costs.format_report(report))


if __name__ == "__main__":
    unittest.main()
