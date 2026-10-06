"""Infraestructura de métricas de uso del LLM (Fase 0).

Registra cada llamada real al LLM en una base SQLite de la librería estándar
(sin SQLAlchemy): tabla `usage_events` (append-only) en `metrics.sqlite3`.

Fase actual: SOLO medición de eventos. NO hay costeo todavía:
  - No se guardan unit_price, cost_amount, country_code, category,
    free_tier_applied ni billable_quantity.
  - Las tarifas por modelo se implementarán en una fase posterior.

La medición es best-effort: si SQLite falla se registra el error vía logging y
el flujo principal del chatbot continúa. Nunca se propaga la excepción al
caller ni se convierte una respuesta válida del LLM en un error HTTP.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger("chatbot.metrics")

# Ruta de la BD de métricas. Se sobreescribe en tests para usar un archivo
# temporal. Este archivo es runtime (gitignored) y regenerable.
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", "metrics.sqlite3")

# Serializa las escrituras dentro del proceso (los workers de uvicorn comparten
# este módulo). SQLite además aplica su propio lock entre procesos.
_WRITE_LOCK = threading.Lock()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    channel TEXT,
    direction TEXT,
    event_type TEXT NOT NULL,
    conversation_id TEXT,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    metadata TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_events_time ON usage_events(event_time);
"""


def _now_iso() -> str:
    """Instante actual en ISO 8601 UTC (con precisión de segundos)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """Abre la conexión y garantiza que la BD y la tabla existan."""
    conn = sqlite3.connect(METRICS_DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn


def record_usage_event(
    tenant_id: str,
    event_type: str,
    provider: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    *,
    channel: str | None = None,
    direction: str = "outbound",
    conversation_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Registra un evento de uso en `usage_events` (best-effort).

    Crea la BD y la tabla si no existen. Los tokens pueden ser None (NULL):
    así se distingue "el proveedor no reportó usage" de "cero tokens". Nunca
    se estiman tokens manualmente. Ante cualquier fallo de SQLite se loguea el
    error y se devuelve sin lanzar excepciones.
    """
    try:
        now = _now_iso()
        payload = {
            "event_time": now,
            "tenant_id": tenant_id,
            "channel": channel,
            "direction": direction,
            "event_type": event_type,
            "conversation_id": conversation_id,
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": now,
        }
        with _WRITE_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO usage_events (
                        event_time, tenant_id, channel, direction, event_type,
                        conversation_id, provider, model, prompt_tokens,
                        completion_tokens, total_tokens, metadata, created_at
                    ) VALUES (
                        :event_time, :tenant_id, :channel, :direction,
                        :event_type, :conversation_id, :provider, :model,
                        :prompt_tokens, :completion_tokens, :total_tokens,
                        :metadata, :created_at
                    )
                    """,
                    payload,
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception(
            "No se pudo registrar el evento de uso (event_type=%s)",
            event_type,
        )