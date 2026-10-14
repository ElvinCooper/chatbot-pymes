"""RAG local E5/ONNX con apertura perezosa y sin recreación destructiva."""

import hashlib
import logging
import os
import json
import subprocess
import sys
import threading
from pathlib import Path

from embeddings import MultilingualE5, MODEL_REVISION
from vector_store import VectorStore

logger = logging.getLogger("chatbot.rag")
RAG_DB_PATH = os.getenv("RAG_DB_PATH", "rag.sqlite3")
COLLECTION_NAME = "business_docs"
CHUNK_SIZE, CHUNK_OVERLAP = 600, 100
MAX_TEXT_CHARS = 300_000
MAX_CHUNKS = 600
_lock = threading.RLock()
_collection = None
_embedding_function = None
COLLECTION_METADATA = {
    "space": "cosine",
    "embedding_model": MultilingualE5.name(),
    "revision": MODEL_REVISION,
}


def _get_collection():
    global _collection, _embedding_function
    with _lock:
        if _collection is not None:
            return _collection
        if _embedding_function is None:
            _embedding_function = MultilingualE5()
        _collection = VectorStore(RAG_DB_PATH, _embedding_function, COLLECTION_METADATA)
        return _collection


def extract_text(file_path: str) -> str:
    path = Path(file_path)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Archivo demasiado grande")
    if path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8")
    elif path.suffix.lower() == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("Firma PDF inválida")
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "pdf_worker.py"), str(path.resolve())],
                capture_output=True,
                check=True,
                timeout=15,
            )
            text = json.loads(result.stdout)["text"]
        except (subprocess.SubprocessError, ValueError, KeyError):
            raise ValueError("No se pudo procesar el PDF de forma segura") from None
    else:
        raise ValueError("Formato no soportado")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("Demasiado texto")
    return text


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if not 0 <= overlap < chunk_size:
        raise ValueError("Solapamiento inválido")
    text = " ".join(text.split())
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size - overlap)]
    if len(chunks) > MAX_CHUNKS:
        raise ValueError("Demasiados fragmentos")
    return chunks


def ingest_document(file_path: str, source_name: str) -> int:
    chunks = chunk_text(extract_text(file_path))
    if not chunks:
        return 0
    digest = hashlib.sha256("\n".join(chunks).encode()).hexdigest()
    with _lock:
        collection = _get_collection()
        collection.replace_source(source_name, digest, chunks)
    return len(chunks)


def retrieve_context(query: str, n_results: int = 5) -> str:
    with _lock:
        collection = _get_collection()
        return "\n\n---\n\n".join(collection.query(query, n_results))[:6000]


def list_sources() -> list[str]:
    with _lock:
        return _get_collection().list_sources()


def delete_source(source_name: str) -> int:
    with _lock:
        return _get_collection().delete_source(source_name)
