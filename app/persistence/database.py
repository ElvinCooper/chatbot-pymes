"""Conexión y esquema compartido de inbox, cuotas y sesiones."""

import contextlib
import sqlite3
from pathlib import Path

from app.core import config

DB_PATH = str(config.INBOX_DB_PATH)
MAX_PENDING = 500
MAX_JOB_HISTORY = 200000
MAX_ATTEMPTS = 3
LEASE_SECONDS = 240


@contextlib.contextmanager
def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            """
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
        """
        )
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
