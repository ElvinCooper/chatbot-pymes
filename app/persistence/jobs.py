"""Cola durable de trabajos, leases y checkpoints."""

import json
import secrets
import time

from app.persistence import database


def enqueue(channel: str, event_id: str, conversation: str, payload: dict) -> bool:
    now = time.time()
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
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
            >= database.MAX_PENDING
        ):
            raise RuntimeError("Cola llena")
        if conn.execute("SELECT count(*) FROM jobs").fetchone()[0] >= database.MAX_JOB_HISTORY:
            raise RuntimeError("Capacidad del historial de dedup agotada")
        conn.execute(
            "INSERT INTO jobs(channel,event_id,conversation,payload,created) VALUES(?,?,?,?,?)",
            (channel, event_id, conversation, json.dumps(payload), now),
        )
        conn.execute("DELETE FROM session_deadlines WHERE conversation=?", (conversation,))
        return True


def claim(channels: tuple[str, ...], now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE jobs SET state='pending',lease=NULL WHERE state='processing' AND lease_until < ?",
            (now,),
        )
        conn.execute(
            "UPDATE jobs SET state='failed',payload='{}',plan=NULL,error='retry_exhausted' WHERE state='pending' AND attempts >= ?",
            (database.MAX_ATTEMPTS,),
        )
        conn.execute(
            "UPDATE jobs SET state='failed',payload='{}',plan=NULL,error='expired' WHERE state='pending' AND created < ?",
            (now - 86400,),
        )
        placeholders = ",".join(("?" for _ in channels))
        row = conn.execute(
            f"SELECT j.* FROM jobs j WHERE j.state='pending' AND j.available<=?\n            AND j.channel IN ({placeholders})\n            AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.conversation=j.conversation\n                AND (p.state='processing' OR (p.state='pending' AND p.id<j.id)))\n            ORDER BY j.id LIMIT 1",
            (now, *channels),
        ).fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        conn.execute(
            "UPDATE jobs SET state='processing',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",
            (token, now + database.LEASE_SECONDS, row["id"]),
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
    with database.connect() as conn:
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
    with database.connect() as conn:
        if error:
            terminal = job["attempts"] >= database.MAX_ATTEMPTS
            conn.execute(
                (
                    "UPDATE jobs SET state=?,lease=NULL,available=?,error=?,payload=CASE WHEN ? THEN '{}' "
                    "ELSE payload END,plan=CASE WHEN ? THEN NULL ELSE plan END WHERE id=? AND lease=?"
                ),
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
