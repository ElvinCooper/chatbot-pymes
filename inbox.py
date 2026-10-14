"""Inbox SQLite: recepción durable, leases, reintentos y cuotas compartidas."""

import contextlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

DB_PATH = os.getenv("INBOX_DB_PATH", "inbox.sqlite3")
MAX_PENDING = 500
MAX_JOB_HISTORY = 200_000
MAX_ATTEMPTS = 3
LEASE_SECONDS = 240


@contextlib.contextmanager
def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, channel TEXT NOT NULL, event_id TEXT NOT NULL,
            conversation TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            lease TEXT, lease_until REAL NOT NULL DEFAULT 0,
            available REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
            plan TEXT, error TEXT, UNIQUE(channel,event_id));
          CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state,available,id);
          CREATE TABLE IF NOT EXISTS quotas (
            scope TEXT NOT NULL, bucket INTEGER NOT NULL, used INTEGER NOT NULL,
            PRIMARY KEY(scope,bucket));
          CREATE TABLE IF NOT EXISTS quota_expiry (
            scope TEXT NOT NULL, bucket INTEGER NOT NULL, expires REAL NOT NULL,
            PRIMARY KEY(scope,bucket));
          CREATE INDEX IF NOT EXISTS quota_expiry_deadline ON quota_expiry(expires);
          CREATE TABLE IF NOT EXISTS sessions (
            conversation TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS session_deadlines (
            conversation TEXT PRIMARY KEY, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS session_locks (
            conversation TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);
        """)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def enqueue(channel: str, event_id: str, conversation: str, payload: dict) -> bool:
    now = time.time()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Conserva IDs durante 7 días, pero borra contenido de mensajes terminados.
        conn.execute(
            "DELETE FROM jobs WHERE state IN ('completed','failed') AND created < ?",
            (now - 7 * 86400,),
        )
        if conn.execute(
            "SELECT 1 FROM jobs WHERE channel=? AND event_id=?", (channel, event_id)
        ).fetchone():
            return False
        if (
            conn.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('pending','processing')"
            ).fetchone()[0]
            >= MAX_PENDING
        ):
            raise RuntimeError("Cola llena")
        if conn.execute("SELECT count(*) FROM jobs").fetchone()[0] >= MAX_JOB_HISTORY:
            raise RuntimeError("Capacidad del historial de dedup agotada")
        conn.execute(
            "INSERT INTO jobs(channel,event_id,conversation,payload,created) VALUES(?,?,?,?,?)",
            (channel, event_id, conversation, json.dumps(payload), now),
        )
        conn.execute("DELETE FROM session_deadlines WHERE conversation=?", (conversation,))
        return True


def claim(channels: tuple[str, ...], now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE jobs SET state='pending',lease=NULL WHERE state='processing' AND lease_until < ?",
            (now,),
        )
        conn.execute(
            "UPDATE jobs SET state='failed',payload='{}',plan=NULL,error='retry_exhausted' WHERE state='pending' AND attempts >= ?",
            (MAX_ATTEMPTS,),
        )
        conn.execute(
            "UPDATE jobs SET state='failed',payload='{}',plan=NULL,error='expired' WHERE state='pending' AND created < ?",
            (now - 86400,),
        )
        placeholders = ",".join("?" for _ in channels)
        row = conn.execute(
            f"""SELECT j.* FROM jobs j WHERE j.state='pending' AND j.available<=?
            AND j.channel IN ({placeholders})
            AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.conversation=j.conversation
                AND (p.state='processing' OR (p.state='pending' AND p.id<j.id)))
            ORDER BY j.id LIMIT 1""",
            (now, *channels),
        ).fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        conn.execute(
            "UPDATE jobs SET state='processing',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",
            (token, now + LEASE_SECONDS, row["id"]),
        )
        result = dict(row)
        result.update(lease=token, attempts=row["attempts"] + 1)
        result["payload"] = json.loads(row["payload"])
        result["plan"] = json.loads(row["plan"]) if row["plan"] else None
        return result


def checkpoint(job: dict, plan: dict):
    encoded = json.dumps(plan)
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("Respuesta demasiado grande")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        total = conn.execute(
            "SELECT coalesce(sum(length(plan)),0) FROM jobs WHERE id!=?", (job["id"],)
        ).fetchone()[0]
        if total + len(encoded) > 50 * 1024 * 1024:
            raise RuntimeError("Capacidad de respuestas pendientes agotada")
        count = conn.execute(
            "UPDATE jobs SET plan=? WHERE id=? AND lease=? AND state='processing'",
            (encoded, job["id"], job["lease"]),
        ).rowcount
        if count != 1:
            raise RuntimeError("Lease perdida")
    job["plan"] = plan


def finish(job: dict, error: str | None = None):
    with connect() as conn:
        if error:
            terminal = job["attempts"] >= MAX_ATTEMPTS
            conn.execute(
                "UPDATE jobs SET state=?,lease=NULL,available=?,error=?,payload=CASE WHEN ? THEN '{}' ELSE payload END,plan=CASE WHEN ? THEN NULL ELSE plan END WHERE id=? AND lease=?",
                (
                    "failed" if terminal else "pending",
                    time.time() + 30 * job["attempts"],
                    error,
                    terminal,
                    terminal,
                    job["id"],
                    job["lease"],
                ),
            )
        else:
            conn.execute(
                "UPDATE jobs SET state='completed',lease=NULL,payload='{}',plan=NULL,error=NULL WHERE id=? AND lease=?",
                (job["id"], job["lease"]),
            )


def reserve(scope: str, limit: int, window: int = 60, amount: int = 1):
    """Reserva antes de consumir. Un fallo de almacenamiento nunca abre la cuota."""
    bucket = int(time.time()) // window
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM quotas WHERE (scope,bucket) IN (SELECT scope,bucket FROM quota_expiry WHERE expires < ?)",
            (time.time(),),
        )
        conn.execute("DELETE FROM quota_expiry WHERE expires < ?", (time.time(),))
        conn.execute("DELETE FROM quotas WHERE bucket < ? AND scope=?", (bucket - 2, scope))
        row = conn.execute(
            "SELECT used FROM quotas WHERE scope=? AND bucket=?", (scope, bucket)
        ).fetchone()
        used = row["used"] if row else 0
        if used + amount > limit:
            raise RuntimeError("Cuota agotada")
        conn.execute(
            "INSERT INTO quotas VALUES(?,?,?) ON CONFLICT(scope,bucket) DO UPDATE SET used=excluded.used",
            (scope, bucket, used + amount),
        )
        conn.execute(
            "INSERT INTO quota_expiry VALUES(?,?,?) ON CONFLICT(scope,bucket) DO NOTHING",
            (scope, bucket, (bucket + 2) * window),
        )


def save_session(conversation: str, data: dict, ttl: int = 600):
    encoded = json.dumps(data)
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("Sesión demasiado grande")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        exists = conn.execute(
            "SELECT 1 FROM sessions WHERE conversation=?", (conversation,)
        ).fetchone()
        if not exists and conn.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 500:
            raise RuntimeError("Capacidad de sesiones agotada")
        total = conn.execute(
            "SELECT coalesce(sum(length(data)),0) FROM sessions WHERE conversation!=?",
            (conversation,),
        ).fetchone()[0]
        if total + len(encoded) > 100 * 1024 * 1024:
            raise RuntimeError("Capacidad de almacenamiento de sesiones agotada")
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(conversation) DO UPDATE SET data=excluded.data,expires=excluded.expires",
            (conversation, encoded, time.time() + ttl),
        )
        conn.execute(
            "INSERT INTO session_deadlines VALUES(?,?) ON CONFLICT(conversation) DO UPDATE SET expires=excluded.expires",
            (conversation, time.time() + ttl),
        )


def load_session(conversation: str) -> dict | None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        row = conn.execute(
            "SELECT data FROM sessions WHERE conversation=?", (conversation,)
        ).fetchone()
        return json.loads(row["data"]) if row else None


def delete_session(conversation: str):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE conversation=?", (conversation,))
        conn.execute("DELETE FROM session_deadlines WHERE conversation=?", (conversation,))


def acquire_session(conversation: str) -> str | None:
    token = secrets.token_hex(16)
    now = time.time()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM session_locks WHERE expires < ?", (now,))
        if conn.execute(
            "SELECT 1 FROM session_locks WHERE conversation=?", (conversation,)
        ).fetchone():
            return None
        conn.execute(
            "INSERT INTO session_locks VALUES(?,?,?)", (conversation, token, now + LEASE_SECONDS)
        )
    return token


def release_session(conversation: str, token: str):
    with connect() as conn:
        conn.execute(
            "DELETE FROM session_locks WHERE conversation=? AND token=?", (conversation, token)
        )


def session_locked(conversation: str) -> bool:
    with connect() as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM session_locks WHERE conversation=? AND expires>?",
                (conversation, time.time()),
            ).fetchone()
        )


def claim_expired_session(conversation: str) -> str | None:
    """Decisión de cierre y lease en la misma transacción; no borra sesiones activas."""
    now = time.time()
    token = secrets.token_hex(16)
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT expires FROM session_deadlines WHERE conversation=?", (conversation,)
        ).fetchone()
        if row is None or row["expires"] > now:
            return None
        lock = conn.execute(
            "SELECT expires FROM session_locks WHERE conversation=?", (conversation,)
        ).fetchone()
        if lock and lock["expires"] > now:
            return None
        conn.execute(
            "INSERT INTO session_locks VALUES(?,?,?) ON CONFLICT(conversation) DO UPDATE SET token=excluded.token,expires=excluded.expires",
            (conversation, token, now + LEASE_SECONDS),
        )
    return token


def expired_sessions(channels: tuple[str, ...]) -> list[str]:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        scopes = (*channels, "api")
        placeholders = ",".join("?" for _ in scopes)
        rows = conn.execute(
            f"SELECT conversation FROM session_deadlines WHERE expires<? AND substr(conversation,1,instr(conversation,':')-1) IN ({placeholders}) LIMIT 500",
            (time.time(), *scopes),
        ).fetchall()
        return [row[0] for row in rows]
