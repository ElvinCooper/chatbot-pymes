"""Infraestructura de métricas de uso (Fases 0 y 2).

Registra eventos en una base SQLite de la librería estándar (sin SQLAlchemy):
tabla `usage_events` (append-only) en `metrics.sqlite3`.

Fase 0: cada llamada real al LLM (event_type `chat_reply` / `quote_extraction`)
con los tokens reportados por el proveedor.

Fase 2: eventos de canal de WhatsApp (`message_received` / `message_sent`) con
los campos `wamid`, `contact_id`, `phone_number_id` y `payload_size`. El
`event_time` se reutiliza (una sola columna): para mensajes entrantes es el
timestamp que reporta Meta (convertido a UTC); para envíos, el momento de
confirmación.

Fase 3: se añaden las tablas de costeo, todas aditivas e idempotentes (nunca
borran ni alteran datos existentes):
  - `tariffs`: tarifas editables (LLM por 1M tokens; WhatsApp por mensaje).
  - `monthly_revenue`: ingresos facturados por tenant y período (YYYY-MM).
  - `whatsapp_message_status`: estado de entrega por wamid (UPSERT idempotente)
    con la categoría de precios de Meta; base del costeo de WhatsApp.

El costeo en sí (fórmulas, tarifas por defecto y el informe) vive en `costs.py`
y `cost_report.py`. `usage_events` sigue siendo append-only.

La medición es best-effort: si SQLite falla se registra el error vía logging y
el flujo principal del chatbot continúa. Nunca se propaga la excepción al
caller ni se convierte una respuesta válida del LLM en un error HTTP.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

from app.core import config

logger = logging.getLogger("chatbot.metrics")
METRICS_DB_PATH = str(config.METRICS_DB_PATH)
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
    wamid TEXT,
    contact_id TEXT,
    phone_number_id TEXT,
    payload_size INTEGER,
    provider TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    metadata TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_events_time ON usage_events(event_time);

CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    market TEXT,
    category TEXT,
    rate REAL,
    rate_input REAL,
    rate_output REAL,
    currency TEXT NOT NULL DEFAULT 'USD',
    unit TEXT,
    effective_from TEXT NOT NULL,
    source_url TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tariffs_lookup
    ON tariffs(kind, provider, model, market, category, effective_from);

CREATE TABLE IF NOT EXISTS monthly_revenue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    period TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, period)
);

CREATE TABLE IF NOT EXISTS whatsapp_message_status (
    wamid TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT,
    contact_id TEXT,
    phone_number_id TEXT,
    status TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    pricing_category TEXT,
    pricing_billable INTEGER,
    pricing_type TEXT,
    country_code TEXT,
    event_time TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_status_time
    ON whatsapp_message_status(event_time);
"""
_ADDED_COLUMNS = {
    "wamid": "TEXT",
    "contact_id": "TEXT",
    "phone_number_id": "TEXT",
    "payload_size": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Migración mínima no destructiva de `usage_events`.

    Comprueba PRAGMA table_info y hace ALTER TABLE ADD COLUMN únicamente para
    las columnas nuevas que no existan (compatible con BDs de Fase 0).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
    for column, ddl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE usage_events ADD COLUMN {column} {ddl}")


def _now_iso() -> str:
    """Instante actual en ISO 8601 UTC (con precisión de segundos)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    """Abre la conexión y garantiza que la BD y la tabla existan (con migración)."""
    conn = sqlite3.connect(METRICS_DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(_SCHEMA_SQL)
    _migrate(conn)
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
    event_time: str | None = None,
    wamid: str | None = None,
    contact_id: str | None = None,
    phone_number_id: str | None = None,
    payload_size: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Registra un evento de uso en `usage_events` (best-effort).

    Crea la BD, la tabla y migra columnas nuevas si no existen. Los tokens y
    los campos de canal pueden ser None (NULL): distinguen "el proveedor no
    reportó usage" / "el envío no devolvió wamid" de un valor real. Nunca se
    estiman tokens manualmente. Ante cualquier fallo de SQLite se loguea el
    error y se devuelve sin lanzar excepciones.

    `event_time` (ISO 8601 UTC) permite fijar el instante real del evento
    (p. ej. el timestamp que reporta Meta); si es None se usa el momento actual.
    """
    try:
        now = _now_iso()
        payload = {
            "event_time": event_time or now,
            "tenant_id": tenant_id,
            "channel": channel,
            "direction": direction,
            "event_type": event_type,
            "conversation_id": conversation_id,
            "wamid": wamid,
            "contact_id": contact_id,
            "phone_number_id": phone_number_id,
            "payload_size": payload_size,
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
                        conversation_id, wamid, contact_id, phone_number_id,
                        payload_size, provider, model, prompt_tokens,
                        completion_tokens, total_tokens, metadata, created_at
                    ) VALUES (
                        :event_time, :tenant_id, :channel, :direction,
                        :event_type, :conversation_id, :wamid, :contact_id,
                        :phone_number_id, :payload_size, :provider, :model,
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
        logger.exception("No se pudo registrar el evento de uso (event_type=%s)", event_type)


def upsert_tariff(
    kind: str,
    provider: str,
    effective_from: str,
    *,
    model: str | None = None,
    market: str | None = None,
    category: str | None = None,
    rate: float | None = None,
    rate_input: float | None = None,
    rate_output: float | None = None,
    currency: str = "USD",
    unit: str | None = None,
    source_url: str | None = None,
    notes: str | None = None,
) -> None:
    """Inserta una tarifa si no existe (best-effort, insert-only).

    Clave lógica: (kind, provider, model, market, category, effective_from).
    Si ya existe una fila con esa clave no se toca, para no pisar ediciones
    manuales hechas directamente sobre la BD. Para cambiar una tarifa hay que
    editar/borrar la fila en la BD (o cambiar el valor por defecto y limpiar).
    """
    try:
        now = _now_iso()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                existing = conn.execute(
                    """
                    SELECT 1 FROM tariffs
                    WHERE kind = ? AND provider = ? AND model IS ?
                      AND market IS ? AND category IS ? AND effective_from = ?
                    """,
                    (kind, provider, model, market, category, effective_from),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO tariffs (
                            kind, provider, model, market, category, rate,
                            rate_input, rate_output, currency, unit,
                            effective_from, source_url, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            kind,
                            provider,
                            model,
                            market,
                            category,
                            rate,
                            rate_input,
                            rate_output,
                            currency,
                            unit,
                            effective_from,
                            source_url,
                            notes,
                            now,
                        ),
                    )
                    conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception("No se pudo registrar la tarifa (provider=%s)", provider)


def set_monthly_revenue(
    tenant_id: str, period: str, amount: float, *, currency: str = "USD", notes: str | None = None
) -> None:
    """Registra/actualiza el ingreso de un tenant para un período (best-effort).

    `period` es 'YYYY-MM'. Upsert por (tenant_id, period): volver a registrar el
    mismo período actualiza el monto en vez de duplicarlo.
    """
    try:
        now = _now_iso()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO monthly_revenue (
                        tenant_id, period, amount, currency, notes,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, period) DO UPDATE SET
                        amount = excluded.amount,
                        currency = excluded.currency,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                    """,
                    (tenant_id, period, amount, currency, notes, now, now),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception(
            "No se pudo registrar el ingreso mensual (tenant=%s, period=%s)", tenant_id, period
        )


def upsert_message_status(
    wamid: str,
    tenant_id: str,
    *,
    conversation_id: str | None = None,
    contact_id: str | None = None,
    phone_number_id: str | None = None,
    status: str | None = None,
    delivered: bool = False,
    failed: bool = False,
    pricing_category: str | None = None,
    pricing_billable: bool | None = None,
    pricing_type: str | None = None,
    country_code: str | None = None,
    event_time: str | None = None,
) -> None:
    """Registra/actualiza el estado de entrega de un mensaje saliente.

    Upsert idempotente por `wamid` (clave primaria): Meta puede enviar varios
    estados para el mismo mensaje (sent → delivered → read) y reintentos. Los
    flags `delivered`/`failed` solo se elevan (MAX), nunca se bajan, y los datos
    de `pricing`/`country_code` se conservan (COALESCE) si un estado posterior no
    los trae. Best-effort: ante fallo de SQLite loguea y no propaga.
    """
    try:
        now = _now_iso()
        with _WRITE_LOCK:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO whatsapp_message_status (
                        wamid, tenant_id, conversation_id, contact_id,
                        phone_number_id, status, delivered, failed,
                        pricing_category, pricing_billable, pricing_type,
                        country_code, event_time, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wamid) DO UPDATE SET
                        status = excluded.status,
                        conversation_id = COALESCE(
                            excluded.conversation_id,
                            whatsapp_message_status.conversation_id
                        ),
                        contact_id = COALESCE(
                            excluded.contact_id,
                            whatsapp_message_status.contact_id
                        ),
                        phone_number_id = COALESCE(
                            excluded.phone_number_id,
                            whatsapp_message_status.phone_number_id
                        ),
                        delivered = MAX(
                            whatsapp_message_status.delivered, excluded.delivered
                        ),
                        failed = MAX(
                            whatsapp_message_status.failed, excluded.failed
                        ),
                        pricing_category = COALESCE(
                            excluded.pricing_category,
                            whatsapp_message_status.pricing_category
                        ),
                        pricing_billable = COALESCE(
                            excluded.pricing_billable,
                            whatsapp_message_status.pricing_billable
                        ),
                        pricing_type = COALESCE(
                            excluded.pricing_type,
                            whatsapp_message_status.pricing_type
                        ),
                        country_code = COALESCE(
                            excluded.country_code,
                            whatsapp_message_status.country_code
                        ),
                        event_time = excluded.event_time,
                        updated_at = excluded.updated_at
                    """,
                    (
                        wamid,
                        tenant_id,
                        conversation_id,
                        contact_id,
                        phone_number_id,
                        status,
                        1 if delivered else 0,
                        1 if failed else 0,
                        pricing_category,
                        None if pricing_billable is None else int(pricing_billable),
                        pricing_type,
                        country_code,
                        event_time,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception("No se pudo registrar el estado del mensaje (wamid=%s)", wamid)
