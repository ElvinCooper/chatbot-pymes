"""Configuración y rutas del proyecto, independientes del directorio de ejecución."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)
env = os.getenv


def resolve_path(value: str | None, default: Path | None = None) -> Path:
    path = Path(value) if value else (default if default is not None else PROJECT_ROOT)
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


RUNTIME_DIR = resolve_path(env("RUNTIME_DIR"), PROJECT_ROOT / "runtime")
DATA_DIR = PROJECT_ROOT / "data"
QUOTE_CATALOG_PATH = resolve_path(env("QUOTE_CATALOG_PATH"), DATA_DIR / "quote_catalog.json")


def runtime_path(variable: str, filename: str) -> Path:
    configured = env(variable)
    if configured:
        return resolve_path(configured)
    legacy = PROJECT_ROOT / filename
    # Nunca abandonar una base existente al cambiar la distribución de carpetas.
    return legacy if legacy.is_file() else RUNTIME_DIR / filename


INBOX_DB_PATH = runtime_path("INBOX_DB_PATH", "inbox.sqlite3")
METRICS_DB_PATH = runtime_path("METRICS_DB_PATH", "metrics.sqlite3")
RAG_DB_PATH = runtime_path("RAG_DB_PATH", "rag.sqlite3")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET") or ""
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
INSTAGRAM_VERIFY_TOKEN = env("INSTAGRAM_VERIFY_TOKEN")
INSTAGRAM_APP_SECRET = env("INSTAGRAM_APP_SECRET") or ""
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN") or ""
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET") or ""
MESSENGER_VERIFY_TOKEN = env("MESSENGER_VERIFY_TOKEN") or ""
MESSENGER_APP_SECRET = env("MESSENGER_APP_SECRET") or ""
IDLE_CLOSE_SECONDS = int(env("IDLE_CLOSE_SECONDS", "600"))
IDLE_CHECK_INTERVAL = int(env("IDLE_CHECK_INTERVAL", "20"))
TENANT_ID = env("TENANT_ID") or "chatbot"
QUOTE_TENANT_ID = TENANT_ID
