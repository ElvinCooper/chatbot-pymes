"""Índice local SQLite transaccional con vectores de datos, sin código dinámico."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

MAX_VECTORS = 20000
DIMENSIONS = 384


class VectorStore:
    def __init__(self, path: str, embedding_function, metadata: dict):
        self.path = path
        self.embedding_function = embedding_function
        self.metadata = metadata
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), metadata TEXT NOT NULL)"
            )
            conn.execute(
                (
                    "CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY,source TEXT NOT NULL, position"
                    " INTEGER NOT NULL,text TEXT NOT NULL,vector BLOB NOT NULL,digest TEXT NOT NULL)"
                )
            )
            conn.execute("CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source)")
            row = conn.execute("SELECT metadata FROM config WHERE id=1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO config VALUES(1,?)", (json.dumps(metadata, sort_keys=True),)
                )
            elif json.loads(row[0]) != metadata:
                raise RuntimeError("Modelo/revisión incompatible; migrar índice sin borrar datos")

    @contextmanager
    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _vectors(values):
        array = np.asarray(values, dtype="<f4")
        if array.ndim != 2 or array.shape[1] != DIMENSIONS or (not np.isfinite(array).all()):
            raise ValueError("Embedding inválido")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        return array / np.maximum(norms, 1e-12)

    def replace_source(self, source: str, digest: str, documents: list[str]):
        with self.connect() as conn:
            rows = conn.execute("SELECT digest FROM chunks WHERE source=?", (source,)).fetchall()
            if len(rows) == len(documents) and all((row[0] == digest for row in rows)):
                return
        vectors = self._vectors(self.embedding_function(documents))
        if len(vectors) != len(documents):
            raise ValueError("Cantidad de embeddings inválida")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            total = conn.execute(
                "SELECT count(*) FROM chunks WHERE source!=?", (source,)
            ).fetchone()[0]
            if total + len(documents) > MAX_VECTORS:
                raise ValueError("Capacidad del índice agotada")
            conn.execute("DELETE FROM chunks WHERE source=?", (source,))
            import hashlib

            source_id = hashlib.sha256(source.encode()).hexdigest()
            conn.executemany(
                "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                [
                    (f"{source_id}-{i}", source, i, text, vector.tobytes(), digest)
                    for i, (text, vector) in enumerate(zip(documents, vectors))
                ],
            )

    def query(self, query: str, n_results: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT text,vector FROM chunks ORDER BY id").fetchall()
        if not rows:
            return []
        query_vector = self._vectors(self.embedding_function.embed_query([query]))[0]
        vectors = self._vectors([np.frombuffer(row[1], dtype="<f4") for row in rows])
        scores = vectors @ query_vector
        best = np.argsort(-scores, kind="stable")[:n_results]
        return [rows[i][0] for i in best]

    def list_sources(self) -> list[str]:
        with self.connect() as conn:
            return [
                row[0] for row in conn.execute("SELECT DISTINCT source FROM chunks ORDER BY source")
            ]

    def delete_source(self, source: str) -> int:
        with self.connect() as conn:
            return conn.execute("DELETE FROM chunks WHERE source=?", (source,)).rowcount
