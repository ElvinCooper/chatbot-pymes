"""
Embedding function multilingüe para ChromaDB basada en ONNX Runtime.

Usa "intfloat/multilingual-e5-small" (226 MB en ONNX optimizado). E5 es un
modelo pensado para *retrieval* multilingüe (50+ idiomas, incluido español) y
supera claramente al MiniLM-L6 por defecto de Chroma en preguntas en español.

Detalle importante: E5 necesita prefijos distintos según el rol del texto
(instrucción oficial de los autores):
    - Documentos a indexar  -> "passage: <texto>"
    - Preguntas de consulta -> "query: <texto>"
Chroma llama a `__call__` al indexar y a `embed_query` al buscar, así que el
prefijo correcto se aplica en cada caso.

El modelo se descarga una sola vez a ~/.cache/huggingface y se ejecuta de
forma local, sin API keys ni servicios externos.
"""

import importlib
from functools import cached_property
from typing import Any, Dict, List, Optional, cast

import numpy as np
import numpy.typing as npt

from chromadb.api.types import Documents, Embeddings, EmbeddingFunction, Space

MODEL_NAME = "intfloat/multilingual-e5-small"
ONNX_FILE = "onnx/model_O4.onnx"
MAX_SEQ_LENGTH = 256
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
MODEL_TAG = "multilingual-e5-small"


class MultilingualE5(EmbeddingFunction[Documents]):
    """EmbeddingFunction de ChromaDB con un E5 multilingüe en ONNX."""

    def __init__(self, preferred_providers: Optional[List[str]] = None) -> None:
        try:
            self._ort = importlib.import_module("onnxruntime")
        except ImportError:
            raise ValueError(
                "El paquete onnxruntime no está instalado. Instálalo con "
                "`pip install onnxruntime`"
            )
        try:
            self._tokenizer_cls = importlib.import_module("tokenizers").Tokenizer
        except ImportError:
            raise ValueError(
                "El paquete tokenizers no está instalado. Instálalo con "
                "`pip install tokenizers`"
            )
        self._preferred_providers = preferred_providers

    # --- Descarga y carga del modelo --------------------------------------

    @cached_property
    def tokenizer(self) -> Any:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id=MODEL_NAME, filename="onnx/tokenizer.json")
        tokenizer = self._tokenizer_cls.from_file(path)
        tokenizer.enable_truncation(max_length=MAX_SEQ_LENGTH)
        tokenizer.enable_padding(pad_id=1, pad_token="<pad>", length=MAX_SEQ_LENGTH)
        return tokenizer

    @cached_property
    def model(self) -> Any:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id=MODEL_NAME, filename=ONNX_FILE)

        so = self._ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = self._ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        providers = self._preferred_providers
        if not providers:
            providers = self._ort.get_available_providers()

        return self._ort.InferenceSession(
            path,
            providers=providers,
            sess_options=so,
        )

    # --- Cómputo -----------------------------------------------------------

    @staticmethod
    def _normalize(v: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        norm = np.linalg.norm(v, axis=1)
        norm[norm == 0] = 1e-12
        return cast(npt.NDArray[np.float32], v / norm[:, np.newaxis])

    def _forward(
        self, documents: List[str], batch_size: int = 32
    ) -> npt.NDArray[np.float32]:
        all_embeddings: List[npt.NDArray[np.float32]] = []
        for i in range(0, len(documents), batch_size):
            batch = documents[i : i + batch_size]
            encoded = [self.tokenizer.encode(doc) for doc in batch]

            for doc_tokens in encoded:
                if len(doc_tokens.ids) > MAX_SEQ_LENGTH:
                    raise ValueError(
                        f"Documento supera el límite de {MAX_SEQ_LENGTH} tokens"
                    )

            input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
            attention_mask = np.array(
                [e.attention_mask for e in encoded], dtype=np.int64
            )
            token_type_ids = np.array(
                [np.zeros(len(e.ids), dtype=np.int64) for e in encoded],
                dtype=np.int64,
            )

            model_output = self.model.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                },
            )
            last_hidden_state = model_output[0]

            input_mask_expanded = np.broadcast_to(
                np.expand_dims(attention_mask, -1), last_hidden_state.shape
            )
            embeddings = np.sum(last_hidden_state * input_mask_expanded, 1) / np.clip(
                input_mask_expanded.sum(1), a_min=1e-9, a_max=None
            )
            embeddings = self._normalize(embeddings).astype(np.float32)
            all_embeddings.append(embeddings)

        return np.concatenate(all_embeddings)

    def __call__(self, input: Documents) -> Embeddings:
        # Al indexar, los documentos se etiquetan como pasajes.
        return cast(
            Embeddings,
            [np.array(emb, dtype=np.float32) for emb in self._forward(
                [f"{PASSAGE_PREFIX}{doc}" for doc in input]
            )],
        )

    def embed_query(self, input: Documents) -> Embeddings:
        # Al buscar, las preguntas llevan el prefijo de consulta.
        return cast(
            Embeddings,
            [np.array(emb, dtype=np.float32) for emb in self._forward(
                [f"{QUERY_PREFIX}{doc}" for doc in input]
            )],
        )

    # --- Configuración de ChromaDB ----------------------------------------

    @staticmethod
    def name() -> str:
        return MODEL_TAG

    def default_space(self) -> Space:
        return "cosine"

    def supported_spaces(self) -> List[Space]:
        return ["cosine", "l2", "ip"]

    def max_tokens(self) -> int:
        return MAX_SEQ_LENGTH

    @staticmethod
    def build_from_config(config: Dict[str, Any]) -> "EmbeddingFunction[Documents]":
        preferred_providers = config.get("preferred_providers")
        return MultilingualE5(preferred_providers=preferred_providers)

    def get_config(self) -> Dict[str, Any]:
        return {"preferred_providers": self._preferred_providers}

    def validate_config_update(
        self, old_config: Dict[str, Any], new_config: Dict[str, Any]
    ) -> None:
        pass

    @staticmethod
    def validate_config(config: Dict[str, Any]) -> None:
        pass