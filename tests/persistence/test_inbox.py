"""Recuperación tras caída, orden entre procesos y checkpoint de envíos."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.integrations.documents as documents_client
import app.persistence.sessions as session_store
import app.rag.indexing as rag
import app.workers.idle as idle_worker
import app.workers.inbox as inbox_worker
from app.core import config, state
from app.integrations.channels import telegram
from app.persistence import database, jobs, metrics, quotas
from app.services import llm, quotes, sessions


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(database, "DB_PATH", str(Path(self.tmp.name) / "inbox.db"))
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)

    def test_dedup_and_lease_recovery(self):
        self.assertTrue(jobs.enqueue("whatsapp", "id", "whatsapp:123", {"message": "hola"}))
        self.assertFalse(jobs.enqueue("whatsapp", "id", "whatsapp:123", {"message": "retry"}))
        job = jobs.claim(("whatsapp",))
        self.assertIsNone(jobs.claim(("whatsapp",)))
        jobs.checkpoint(job, {"kind": "text", "text": "ok"})
        recovered = jobs.claim(("whatsapp",), time.time() + database.LEASE_SECONDS + 1)
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["plan"]["text"], "ok")
        jobs.finish(job)
        with database.connect() as conn:
            self.assertEqual(conn.execute("SELECT state FROM jobs").fetchone()[0], "processing")
        jobs.finish(recovered)
        self.assertIsNone(jobs.claim(("whatsapp",)))

    def test_conversation_order_and_terminal_failure(self):
        for event in ("1", "2"):
            jobs.enqueue("telegram", event, "telegram:123", {})
        first = jobs.claim(("telegram",))
        self.assertIsNone(jobs.claim(("telegram",)))
        jobs.finish(first, "network")
        self.assertIsNone(jobs.claim(("telegram",)))
        first = jobs.claim(("telegram",), time.time() + 100)
        jobs.finish(first)
        second = jobs.claim(("telegram",))
        self.assertEqual(second["event_id"], "2")
        second["attempts"] = database.MAX_ATTEMPTS
        jobs.finish(second, "network")
        with database.connect() as conn:
            row = conn.execute(
                "SELECT state,payload,plan FROM jobs WHERE id=?", (second["id"],)
            ).fetchone()
            self.assertEqual(tuple(row), ("failed", "{}", None))

    def test_storage_error_does_not_acknowledge_message(self):
        with mock.patch.object(database, "connect", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                jobs.enqueue("telegram", "1", "telegram:123", {})

    def test_shared_quota_and_session_expiry(self):
        with mock.patch.object(quotas, "time") as quota_clock:
            quota_clock.time.return_value = 1200.0
            quotas.reserve("client", 2)
            quotas.reserve("client", 2)
            with self.assertRaises(RuntimeError):
                quotas.reserve("client", 2)
        session_store.save_session("api:1", {"history": ["dato"]}, ttl=-1)
        self.assertIsNone(session_store.load_session("api:1"))

    def test_quota_remains_exhausted_until_next_fixed_window(self):
        with mock.patch.object(quotas, "time") as quota_clock:
            quota_clock.time.return_value = 1200.0
            for _ in range(20):
                quotas.reserve("client", 20)
            quota_clock.time.return_value = 1259.999
            with self.assertRaises(RuntimeError):
                quotas.reserve("client", 20)
            quota_clock.time.return_value = 1260.0
            for _ in range(20):
                quotas.reserve("client", 20)
            with self.assertRaises(RuntimeError):
                quotas.reserve("client", 20)

    def test_session_lease_serializes_independent_consumers(self):
        token = session_store.acquire_session("api:1")
        self.assertIsNotNone(token)
        self.assertIsNone(session_store.acquire_session("api:1"))
        session_store.release_session("api:1", "wrong-token")
        self.assertIsNone(session_store.acquire_session("api:1"))
        session_store.release_session("api:1", token)
        self.assertIsNotNone(session_store.acquire_session("api:1"))

    def test_close_cannot_claim_active_session_or_busy_conversation(self):
        session_store.save_session("api:close", {}, ttl=600)
        self.assertIsNone(session_store.claim_expired_session("api:close"))
        session_store.save_session("api:close", {}, ttl=-1)
        token = session_store.acquire_session("api:close")
        self.assertIsNone(session_store.claim_expired_session("api:close"))
        session_store.release_session("api:close", token)
        self.assertIsNotNone(session_store.claim_expired_session("api:close"))


class ProcessingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(database, "DB_PATH", str(Path(self.tmp.name) / "inbox.db"))
        patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stop)

    async def test_checkpoint_replays_without_llm_and_accepted_not_resent(self):
        jobs.enqueue("telegram", "1", "telegram:123", {"target": "123", "message": "hola"})
        job = jobs.claim(("telegram",))
        jobs.checkpoint(job, {"kind": "text", "text": "respuesta"})
        with (
            mock.patch.object(
                telegram, "send_message", new=mock.AsyncMock(return_value=True)
            ) as send,
            mock.patch.object(llm, "try_provider", new=mock.AsyncMock()) as llm_mock,
        ):
            await inbox_worker.handle_channel_message(job)
            await inbox_worker.handle_channel_message(job)
            send.assert_awaited_once_with(123, "respuesta")
            llm_mock.assert_not_called()

    async def test_idle_failure_does_not_prevent_other_conversation_cleanup(self):
        for cid in ["telegram:401", "telegram:402"]:
            session_store.save_session(cid, {}, ttl=-1)
            state.session_activity[cid] = time.monotonic() - config.IDLE_CLOSE_SECONDS - 1
            state.idle_target_hint[cid] = cid.split(":")[1]
        with mock.patch.object(
            telegram,
            "send_message",
            new=mock.AsyncMock(side_effect=[RuntimeError("network"), True]),
        ) as send:
            await idle_worker.expire_conversation("telegram:401")
            await idle_worker.expire_conversation("telegram:402")
        self.assertEqual(send.await_count, 2)
        for cid in ["telegram:401", "telegram:402"]:
            self.assertNotIn(cid, state.session_activity)
            self.assertIsNone(session_store.load_session(cid))

    async def test_replay_restores_pending_state_and_does_not_duplicate_history(self):
        cid = "telegram:123"
        session_store.save_session(cid, {"history": [], "pending": True})
        jobs.enqueue("telegram", "social-1", cid, {"target": "123", "message": "gracias"})
        job = jobs.claim(("telegram",))
        pair = [{"role": "user", "content": "gracias"}, {"role": "assistant", "content": "De nada"}]
        jobs.checkpoint(
            job, {"kind": "text", "text": "De nada", "session": {"history": pair, "pending": False}}
        )
        with mock.patch.object(telegram, "send_message", new=mock.AsyncMock(return_value=True)):
            await inbox_worker.handle_channel_message(job)
            await inbox_worker.handle_channel_message(job)
        saved = session_store.load_session(cid)
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
            mock.patch.object(rag, "retrieve_context", return_value=""),
            mock.patch.object(
                quotes, "extract_quote_draft", new=mock.AsyncMock(return_value=draft)
            ),
            mock.patch.object(
                quotes,
                "materialize_quote",
                new=mock.AsyncMock(return_value=(b"%PDF-1.4", "x.pdf", "caption")),
            ),
        ):

            async def fail(*args):
                raise RuntimeError("falló el canal")

            sessions.close_conversation("telegram:123")
            with self.assertRaises(RuntimeError):
                await quotes.run_quote_flow(
                    "cotización", "telegram:123", "", mock.AsyncMock(), fail
                )
            self.assertNotIn("telegram:123", state.last_quote_pdf)

    async def test_quote_end_to_end_uses_authoritative_price_and_stable_id(self):
        jobs.enqueue(
            "telegram",
            "quote-1",
            "telegram:123",
            {"target": "123", "message": "cotización de 2 panes franceses"},
        )
        job = jobs.claim(("telegram",))
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
        result = documents_client.QuoteResult(
            "doc1", "COT-001", "https://documents.test/files/x.pdf", 12, "ready"
        )
        with (
            mock.patch.object(rag, "retrieve_context", return_value="Pan francés RD$35"),
            mock.patch.object(
                llm,
                "try_provider",
                new=mock.AsyncMock(return_value=llm.LLMResult(json.dumps(draft), "mock", "mock")),
            ),
            mock.patch.object(metrics, "record_usage_event"),
            mock.patch.object(
                documents_client, "create_quote", new=mock.AsyncMock(return_value=result)
            ) as create,
            mock.patch.object(
                documents_client,
                "fetch_quote_pdf",
                new=mock.AsyncMock(return_value=b"%PDF-1.4 fake"),
            ),
            mock.patch.object(
                telegram, "send_document", new=mock.AsyncMock(return_value=True)
            ) as send,
        ):
            await inbox_worker.handle_channel_message(job)
            self.assertEqual(create.call_args.kwargs["items"][0]["unit_price"], "35.00")
            request_id = create.call_args.kwargs["request_id"]
            self.assertTrue(request_id.startswith("chat-"))
            send.assert_awaited_once()
            await inbox_worker.handle_channel_message(job)
            self.assertEqual(create.await_count, 1)
            self.assertEqual(send.await_count, 1)
            self.assertIsNotNone(session_store.load_session("telegram:123")["pdf"])
