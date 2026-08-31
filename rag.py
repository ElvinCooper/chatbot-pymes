"""
Módulo de RAG (Retrieval-Augmented Generation) para el chatbot.

Usa ChromaDB con una función de embeddings local multilingüe (MiniLM-L12 vía
ONNX), así que no necesita ninguna API key ni conexión externa para generar los
embeddings (a excepción de la primera descarga del modelo): todo corre en tu
propio servidor, gratis.

Soporta ingesta de archivos .txt y .pdf.
"""

import logging
import uuid
from pathlib import Path

import chromadb
from pypdf import PdfReader

from embeddings import MultilingualE5

logger = logging.getLogger("chatbot.rag")

CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "business_docs"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 100

_client = chromadb.PersistentClient(path=CHROMA_PATH)
_embedding_function = MultilingualE5()
COLLECTION_METADATA = {
    "hnsw:space": "cosine",
    "embedding_model": MultilingualE5.name(),
}


def _get_collection():
    """Crea o reabre la colección. Si ya existía con otro modelo de embeddings o
    con un espacio de distancia distinto del coseno, la recrea para evitar
    vectores incompatibles y ranking incorrecto."""
    try:
        existing = _client.get_collection(
            COLLECTION_NAME, embedding_function=_embedding_function
        )
        metadata = existing.metadata or {}
        if metadata.get("hnsw:space") != "cosine":
            raise ValueError("la colección usa un espacio de distancia distinto de coseno")
        if metadata.get("embedding_model") != COLLECTION_METADATA["embedding_model"]:
            raise ValueError("la colección usa otro modelo de embeddings")
        return existing
    except Exception:
        logger.warning(
            "Colección '%s' incompatible con el modelo de embeddings/espacio actual. "
            "Se recrea; los documentos habrá que volver a indexarlos.",
            COLLECTION_NAME,
        )
        try:
            _client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
        return _client.create_collection(
            COLLECTION_NAME,
            embedding_function=_embedding_function,
            metadata=COLLECTION_METADATA,
        )


_collection = _get_collection()


def extract_text(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore")

    if suffix == ".pdf":
        reader = PdfReader(file_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    raise ValueError(f"Formato no soportado: {suffix}. Usa .txt o .pdf")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Divide el texto en fragmentos solapados para preservar contexto entre cortes."""
    text = " ".join(text.split())  # normaliza espacios y saltos de línea
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return [c for c in chunks if c.strip()]


def ingest_document(file_path: str, source_name: str) -> int:
    """Extrae, trocea e indexa un documento. Devuelve el número de chunks creados."""
    text = extract_text(file_path)
    chunks = chunk_text(text)

    if not chunks:
        return 0

    ids = [f"{source_name}-{uuid.uuid4().hex[:8]}-{i}" for i in range(len(chunks))]
    metadatas = [{"source": source_name, "chunk_index": i} for i in range(len(chunks))]

    _collection.add(documents=chunks, ids=ids, metadatas=metadatas)
    return len(chunks)


def retrieve_context(query: str, n_results: int = 5) -> str:
    """Busca los fragmentos más relevantes para la pregunta del usuario."""
    if _collection.count() == 0:
        return ""

    results = _collection.query(
        query_texts=[query],
        n_results=min(n_results, _collection.count()),
    )
    docs = results.get("documents", [[]])[0]
    return "\n\n---\n\n".join(docs)


def list_sources() -> list[str]:
    if _collection.count() == 0:
        return []
    data = _collection.get()
    sources = {m["source"] for m in data.get("metadatas", [])}
    return sorted(sources)


def delete_source(source_name: str) -> int:
    """Elimina todos los chunks de un documento específico. Devuelve cuántos borró."""
    existing = _collection.get(where={"source": source_name})
    ids = existing.get("ids", [])
    if ids:
        _collection.delete(ids=ids)
    return len(ids)
