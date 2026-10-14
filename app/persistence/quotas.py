"""Reservas de cuotas globales en SQLite."""

import time

from app.persistence import database


def reserve(scope: str, limit: int, window: int = 60, amount: int = 1):
    """Reserva antes de consumir. Un fallo de almacenamiento nunca abre la cuota."""
    bucket = int(time.time()) // window
    with database.connect() as conn:
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
