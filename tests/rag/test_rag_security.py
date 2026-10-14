"""Índice SQLite real, embeddings ficticios deterministas y parser PDF aislado."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import app.rag.indexing as rag
from app.persistence.vector_store import VectorStore


class FakeEmbedding:
    def __call__(self, documents):
        result = []
        for text in documents:
            value = np.zeros(384, dtype=np.float32)
            value[0 if "pan" in text else 1] = 1
            result.append(value)
        return result

    def embed_query(self, documents):
        return self(documents)


class RagTest(unittest.TestCase):
    def test_valid_pdf_parser_runs_in_isolated_process(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with path.open("wb") as stream:
                writer.write(stream)
            self.assertEqual(rag.extract_text(str(path)), "")

    def test_transient_error_and_incompatibility_never_delete_data(self):
        with (
            mock.patch.object(rag, "VectorStore", side_effect=OSError("timeout")),
            mock.patch.object(rag, "_collection", None),
            mock.patch.object(rag, "_embedding_function", FakeEmbedding()),
        ):
            with self.assertRaises(OSError):
                rag._get_collection()
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "vectors.db")
            store = VectorStore(path, FakeEmbedding(), {"model": "a"})
            store.replace_source("x.txt", "digest", ["pan francés"])
            with self.assertRaises(RuntimeError):
                VectorStore(path, FakeEmbedding(), {"model": "b"})
            self.assertEqual(store.list_sources(), ["x.txt"])
            self.assertEqual(store.query("pan", 1), ["pan francés"])

    def test_same_source_reingestion_idempotent_and_replaces_changed_content(self):
        with tempfile.TemporaryDirectory() as folder:
            store = VectorStore(
                str(Path(folder) / "vectors.db"), FakeEmbedding(), rag.COLLECTION_METADATA
            )
            with mock.patch.object(rag, "_get_collection", return_value=store):
                path = Path(folder) / "x.txt"
                path.write_text("dato antiguo", encoding="utf-8")
                self.assertEqual(rag.ingest_document(str(path), "x.txt"), 1)
                rag.ingest_document(str(path), "x.txt")
                with store.connect() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM chunks").fetchone()[0], 1)
                path.write_text("dato nuevo", encoding="utf-8")
                rag.ingest_document(str(path), "x.txt")
                self.assertEqual(store.query("dato", 10), ["dato nuevo"])

    def test_cosine_retrieval_delete_and_failed_embedding_preserve_source(self):
        with tempfile.TemporaryDirectory() as folder:
            embedding = FakeEmbedding()
            store = VectorStore(str(Path(folder) / "vectors.db"), embedding, {})
            store.replace_source("pan.txt", "a", ["pan francés"])
            store.replace_source("cafe.txt", "b", ["café de la casa"])
            self.assertEqual(store.query("pan", 1), ["pan francés"])
            with mock.patch.object(
                store, "embedding_function", side_effect=RuntimeError("modelo falló")
            ):
                with self.assertRaises(RuntimeError):
                    store.replace_source("pan.txt", "c", ["nuevo pan"])
            self.assertEqual(store.query("pan", 1), ["pan francés"])
            self.assertEqual(store.delete_source("pan.txt"), 1)
            self.assertEqual(store.list_sources(), ["cafe.txt"])

    def test_bad_pdf_and_chunk_settings_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.pdf"
            path.write_bytes(b"not a pdf")
            with self.assertRaises(ValueError):
                rag.extract_text(str(path))
        with self.assertRaises(ValueError):
            rag.chunk_text("x", 10, 10)
