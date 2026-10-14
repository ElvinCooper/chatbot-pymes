"""Descarga sin red real: origen, credencial, firma, tipo y límite de bytes."""

import os
import unittest
from unittest import mock
import httpx
import document_service


class DocumentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_quote_transport_preserves_numeric_contract(self):
        import json

        def handler(request):
            payload = json.loads(request.content)
            self.assertIsInstance(payload["items"][0]["unit_price"], float)
            self.assertEqual(payload["items"][0]["unit_price"], 35.0)
            self.assertEqual(payload["items"][0]["quantity"], 2.0)
            return httpx.Response(
                201,
                json={
                    "document_id": "doc1",
                    "quote_number": "COT-1",
                    "status": "ready",
                    "files": [{"format": "pdf", "url": "/files/x.pdf", "size_bytes": 12}],
                },
            )

        factory = httpx.AsyncClient
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DOCUMENT_SERVICE_URL": "https://documents.test",
                    "INTERNAL_API_KEY": "internal-test",
                },
            ),
            mock.patch.object(
                document_service.httpx,
                "AsyncClient",
                side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw),
            ),
        ):
            result = await document_service.create_quote(
                "stable-id",
                "chatbot",
                {"name": "Cliente"},
                [{"description": "Pan francés", "quantity": "2", "unit_price": "35.00"}],
            )
            self.assertEqual(result.quote_number, "COT-1")

    async def fetch(self, response, url="https://documents.test/files/x.pdf"):
        def handler(request):
            self.assertEqual(request.headers["X-Internal-API-Key"], "internal-test")
            return response

        factory = httpx.AsyncClient
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DOCUMENT_SERVICE_URL": "https://documents.test",
                    "INTERNAL_API_KEY": "internal-test",
                },
            ),
            mock.patch.object(
                document_service.httpx,
                "AsyncClient",
                side_effect=lambda **kw: factory(transport=httpx.MockTransport(handler), **kw),
            ),
        ):
            return await document_service.fetch_quote_pdf(url)

    async def test_pdf_download_allowed(self):
        self.assertEqual(
            await self.fetch(
                httpx.Response(
                    200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"}
                )
            ),
            b"%PDF-1.4 fake",
        )

    async def test_download_rejects_other_origin_redirect_html_or_oversize(self):
        for response in [
            httpx.Response(302, headers={"location": "http://127.0.0.1/"}),
            httpx.Response(200, content=b"html", headers={"content-type": "text/html"}),
            httpx.Response(200, content=b"not pdf", headers={"content-type": "application/pdf"}),
            httpx.Response(
                200,
                content=b"%PDF-",
                headers={
                    "content-type": "application/pdf",
                    "content-length": str(document_service.MAX_PDF_BYTES + 1),
                },
            ),
        ]:
            self.assertIsNone(await self.fetch(response))
        self.assertIsNone(
            await self.fetch(httpx.Response(200), url="http://169.254.169.254/latest/")
        )

    async def test_chunked_response_cannot_bypass_limit(self):
        with mock.patch.object(document_service, "MAX_PDF_BYTES", 10):
            self.assertIsNone(
                await self.fetch(
                    httpx.Response(
                        200,
                        content=b"%PDF-" + b"x" * 20,
                        headers={"content-type": "application/pdf"},
                    )
                )
            )

    def test_url_response_validation(self):
        for value in [
            "https://attacker.test/a",
            "//attacker.test/a",
            "/../secret",
            "/a/%2e%2e/secret",
        ]:
            with self.assertRaises(ValueError):
                document_service._build_pdf_url("https://documents.test", value)
        self.assertIsNone(
            document_service._parse_quote_response("https://documents.test", {"files": "bad"})
        )
