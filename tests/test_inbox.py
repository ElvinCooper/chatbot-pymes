"""Recuperación tras caída, orden entre procesos y checkpoint de envíos."""

import os
from pathlib import Path
import tempfile
import time
import json
import unittest
from unittest import mock
import inbox
import main


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(inbox, "DB_PATH", str(Path(self.tmp.name) / "inbox.db"))
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)

    def test_dedup_and_lease_recovery(self):
        self.assertTrue(inbox.enqueue("whatsapp", "id", "whatsapp:123", {"message": "hola"}))
        self.assertFalse(inbox.enqueue("whatsapp", "id", "whatsapp:123", {"message": "retry"}))
        job = inbox.claim(("whatsapp",))
        self.assertIsNone(inbox.claim(("whatsapp",)))
        inbox.checkpoint(job, {"kind": "text", "text": "ok"})
        recovered = inbox.claim(("whatsapp",), time.time() + inbox.LEASE_SECONDS + 1)
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["plan"]["text"], "ok")
        inbox.finish(job)  # El worker viejo no puede terminar la lease nueva.
        with inbox.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM jobs").fetchone()[0], "processing")
        inbox.finish(recovered)
        self.assertIsNone(inbox.claim(("whatsapp",)))

    def test_conversation_order_and_terminal_failure(self):
        for event in ("1", "2"):
            inbox.enqueue("telegram", event, "telegram:123", {})
        first = inbox.claim(("telegram",))
        self.assertIsNone(inbox.claim(("telegram",)))
        inbox.finish(first, "network")
        self.assertIsNone(inbox.claim(("telegram",)))
        first = inbox.claim(("telegram",), time.time() + 100)
        inbox.finish(first)
        second = inbox.claim(("telegram",))
        self.assertEqual(second["event_id"], "2")
        second["attempts"] = inbox.MAX_ATTEMPTS
        inbox.finish(second, "network")
        with inbox.connect() as conn:
            row = conn.execute(
                "SELECT state,payload,plan FROM jobs WHERE id=?", (second["id"],)
            ).fetchone()
            self.assertEqual(tuple(row), ("failed", "{}", None))

    def test_storage_error_does_not_acknowledge_message(self):
        with mock.patch.object(inbox, "connect", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                inbox.enqueue("telegram", "1", "telegram:123", {})

    def test_shared_quota_and_session_expiry(self):
        inbox.reserve("client", 2)
        inbox.reserve("client", 2)
        with self.assertRaises(RuntimeError):
            inbox.reserve("client", 2)
        inbox.save_session("api:1", {"history": ["dato"]}, ttl=-1)
        self.assertIsNone(inbox.load_session("api:1"))

    def test_session_lease_serializes_independent_consumers(self):
        token = inbox.acquire_session("api:1")
        self.assertIsNotNone(token)
        self.assertIsNone(inbox.acquire_session("api:1"))
        inbox.release_session("api:1", "wrong-token")
        self.assertIsNone(inbox.acquire_session("api:1"))
        inbox.release_session("api:1", token)
        self.assertIsNotNone(inbox.acquire_session("api:1"))

    def test_close_cannot_claim_active_session_or_busy_conversation(self):
        inbox.save_session("api:close", {}, ttl=600)
        self.assertIsNone(inbox.claim_expired_session("api:close"))
        inbox.save_session("api:close", {}, ttl=-1)
        token = inbox.acquire_session("api:close")
        self.assertIsNone(inbox.claim_expired_session("api:close"))
        inbox.release_session("api:close", token)
        self.assertIsNotNone(inbox.claim_expired_session("api:close"))


class ProcessingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(inbox, "DB_PATH", str(Path(self.tmp.name) / "inbox.db"))
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)

    async def test_checkpoint_replays_without_llm_and_accepted_not_resent(self):
        inbox.enqueue("telegram", "1", "telegram:123", {"target": "123", "message": "hola"})
        job = inbox.claim(("telegram",))
        inbox.checkpoint(job, {"kind": "text", "text": "respuesta"})
        with (
            mock.patch.object(
                main.telegram_adapter, "send_message", new=mock.AsyncMock(return_value=True)
            ) as send,
            mock.patch.object(main, "_try_provider", new=mock.AsyncMock()) as llm,
        ):
            await main._handle_channel_message(job)
            await main._handle_channel_message(job)
            send.assert_awaited_once_with(123, "respuesta")
            llm.assert_not_called()

    async def test_idle_failure_does_not_prevent_other_conversation_cleanup(self):
        for cid in ["telegram:401", "telegram:402"]:
            inbox.save_session(cid, {}, ttl=-1)
            main._session_activity[cid] = time.monotonic() - main.IDLE_CLOSE_SECONDS - 1
            main._idle_target_hint[cid] = cid.split(":")[1]
        with mock.patch.object(
            main.telegram_adapter,
            "send_message",
            new=mock.AsyncMock(side_effect=[RuntimeError("network"), True]),
        ) as send:
            await main._expire_conversation("telegram:401")
            await main._expire_conversation("telegram:402")
        self.assertEqual(send.await_count, 2)
        for cid in ["telegram:401", "telegram:402"]:
            self.assertNotIn(cid, main._session_activity)
            self.assertIsNone(inbox.load_session(cid))

    async def test_replay_restores_pending_state_and_does_not_duplicate_history(self):
        cid = "telegram:123"
        inbox.save_session(cid, {"history": [], "pending": True})
        inbox.enqueue("telegram", "social-1", cid, {"target": "123", "message": "gracias"})
        job = inbox.claim(("telegram",))
        pair = [{"role": "user", "content": "gracias"}, {"role": "assistant", "content": "De nada"}]
        inbox.checkpoint(
            job, {"kind": "text", "text": "De nada", "session": {"history": pair, "pending": False}}
        )
        with mock.patch.object(
            main.telegram_adapter, "send_message", new=mock.AsyncMock(return_value=True)
        ):
            await main._handle_channel_message(job)
            await main._handle_channel_message(job)
        saved = inbox.load_session(cid)
        self.assertFalse(saved["pending"])
        self.assertEqual(saved["history"], pair)

    async def test_failed_document_is_not_recorded_as_sent(self):
        draft = {
            "intent": "quote",
            "items": [{"description": "Pan francés", "quantity": 1, "unit_price": 0}],
            "customer": {"name": "Cliente"},
            "currency": "DOP",
        }
        with (
            mock.patch.object(main.rag, "retrieve_context", return_value=""),
            mock.patch.object(main, "_extract_quote_draft", new=mock.AsyncMock(return_value=draft)),
            mock.patch.object(
                main,
                "_materialize_quote",
                new=mock.AsyncMock(return_value=(b"%PDF-1.4", "x.pdf", "caption")),
            ),
        ):

            async def fail(*args):
                raise RuntimeError("falló el canal")

            main._close_conversation("telegram:123")
            with self.assertRaises(RuntimeError):
                await main._run_quote_flow("cotización", "telegram:123", "", mock.AsyncMock(), fail)
            self.assertNotIn("telegram:123", main._last_quote_pdf)

    async def test_quote_end_to_end_uses_authoritative_price_and_stable_id(self):
        inbox.enqueue(
            "telegram",
            "quote-1",
            "telegram:123",
            {"target": "123", "message": "cotización de 2 panes franceses"},
        )
        job = inbox.claim(("telegram",))
        draft = {
            "intent": "quote",
            "customer": {"name": "Cliente"},
            "currency": "DOP",
            "items": [
                {
                    "product_id": "pan_frances",
                    "description": "Pan francés",
                    "quantity": 2,
                    "unit_price": 0.01,
                }
            ],
        }
        result = main.document_service.QuoteResult(
            "doc1", "COT-001", "https://documents.test/files/x.pdf", 12, "ready"
        )
        with (
            mock.patch.object(main.rag, "retrieve_context", return_value="Pan francés RD$35"),
            mock.patch.object(
                main,
                "_try_provider",
                new=mock.AsyncMock(return_value=main.LLMResult(json.dumps(draft), "mock", "mock")),
            ),
            mock.patch.object(main.metrics, "record_usage_event"),
            mock.patch.object(
                main.document_service, "create_quote", new=mock.AsyncMock(return_value=result)
            ) as create,
            mock.patch.object(
                main.document_service,
                "fetch_quote_pdf",
                new=mock.AsyncMock(return_value=b"%PDF-1.4 fake"),
            ),
            mock.patch.object(
                main.telegram_adapter, "send_document", new=mock.AsyncMock(return_value=True)
            ) as send,
        ):
            await main._handle_channel_message(job)
            self.assertEqual(create.call_args.kwargs["items"][0]["unit_price"], "35.00")
            request_id = create.call_args.kwargs["request_id"]
            self.assertTrue(request_id.startswith("chat-"))
            send.assert_awaited_once()
            await main._handle_channel_message(job)
            self.assertEqual(create.await_count, 1)
            self.assertEqual(send.await_count, 1)
            self.assertIsNotNone(inbox.load_session("telegram:123")["pdf"])
