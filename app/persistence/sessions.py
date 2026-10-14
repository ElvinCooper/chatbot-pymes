"""Contenido de sesiones, vencimientos y leases en SQLite."""

import json
import secrets
import time

from app.persistence import database


def save_session(conversation: str, data: dict, ttl: int = 600):
    encoded = json.dumps(data)
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("Sesión demasiado grande")
    with database.connect() as conn:
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
    with database.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        row = conn.execute(
            "SELECT data FROM sessions WHERE conversation=?", (conversation,)
        ).fetchone()
        return json.loads(row["data"]) if row else None


def delete_session(conversation: str):
    with database.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE conversation=?", (conversation,))
        conn.execute("DELETE FROM session_deadlines WHERE conversation=?", (conversation,))


def acquire_session(conversation: str) -> str | None:
    token = secrets.token_hex(16)
    now = time.time()
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM session_locks WHERE expires < ?", (now,))
        if conn.execute(
            "SELECT 1 FROM session_locks WHERE conversation=?", (conversation,)
        ).fetchone():
            return None
        conn.execute(
            "INSERT INTO session_locks VALUES(?,?,?)",
            (conversation, token, now + database.LEASE_SECONDS),
        )
    return token


def release_session(conversation: str, token: str):
    with database.connect() as conn:
        conn.execute(
            "DELETE FROM session_locks WHERE conversation=? AND token=?", (conversation, token)
        )


def session_locked(conversation: str) -> bool:
    with database.connect() as conn:
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
    with database.connect() as conn:
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
            (conversation, token, now + database.LEASE_SECONDS),
        )
    return token


def expired_sessions(channels: tuple[str, ...]) -> list[str]:
    with database.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        scopes = (*channels, "api")
        placeholders = ",".join(("?" for _ in scopes))
        rows = conn.execute(
            f"SELECT conversation FROM session_deadlines WHERE expires<? AND substr(conversation,1,instr(conversation,':')-1) IN ({placeholders}) LIMIT 500",
            (time.time(), *scopes),
        ).fetchall()
        return [row[0] for row in rows]
