"""Configuración de datos sin shell e idempotencia/adjuntos de correo."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock
import email_service
import load_env


class ConfigTest(unittest.TestCase):
    def test_dotenv_never_evaluates_shell_or_interpolates_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "run_bot.sh"
            shell = root / "bash"
            script.write_text("placeholder", encoding="utf-8")
            shell.write_text("placeholder", encoding="utf-8")
            (root / ".env").write_text(
                "VALUE='$(touch injected)'\nOTHER='${VALUE}'\n", encoding="utf-8"
            )
            with (
                mock.patch.object(load_env, "__file__", str(root / "load_env.py")),
                mock.patch.object(
                    load_env.sys, "argv", ["load_env.py", str(shell), str(script), "start"]
                ),
                mock.patch.object(load_env.subprocess, "call", return_value=0) as call,
            ):
                self.assertEqual(load_env.main(), 0)
                call.assert_called_once()
                self.assertEqual(call.call_args.kwargs["env"]["VALUE"], "$(touch injected)")
                self.assertEqual(call.call_args.kwargs["env"]["OTHER"], "${VALUE}")
                self.assertNotIn("shell", call.call_args.kwargs)
                self.assertFalse((root / "injected").exists())


class EmailTest(unittest.IsolatedAsyncioTestCase):
    async def test_idempotency_and_attachments_are_checked_before_network(self):
        with (
            mock.patch.dict(
                os.environ,
                {"EMAIL_SERVICE_URL": "https://mail.test", "EMAIL_SERVICE_API_KEY": "test-key"},
            ),
            mock.patch.object(email_service.httpx, "AsyncClient") as client,
        ):
            self.assertIsNone(await email_service.send_email("chatbot", "x@test.test", "test"))
            self.assertIsNone(
                await email_service.send_email(
                    "chatbot",
                    "x@test.test",
                    "test",
                    idempotency_key="stable",
                    attachments=[{"content_base64": "invalid!"}],
                )
            )
            client.assert_not_called()

    def test_bad_email_service_response_and_large_encoding_are_controlled(self):
        for data in [[], {"success": False, "error": "bad"}, {"success": True, "email_id": 123}]:
            self.assertIsNone(email_service._parse_email_response(data))
        with mock.patch.object(email_service, "MAX_ATTACHMENT_BYTES", 5):
            with self.assertRaises(ValueError):
                email_service.encode_attachment("x.pdf", b"0123456", "application/pdf")
