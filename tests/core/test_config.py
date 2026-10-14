"""Rutas estables y compatibilidad de almacenamiento tras la reorganización."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import config
from app.services import catalog


class PathsTest(unittest.TestCase):
    def test_catalog_remains_available_after_changing_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path.cwd()
            try:
                os.chdir(folder)
                with mock.patch.dict(os.environ, {"QUOTE_CATALOG_PATH": "data/quote_catalog.json"}):
                    self.assertIn("pan_frances", catalog.products())
                self.assertEqual(config.resolve_path("data"), config.PROJECT_ROOT / "data")
            finally:
                os.chdir(original)

    def test_existing_root_database_wins_over_new_runtime_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / "inbox.sqlite3"
            legacy.write_bytes(b"existing database")
            with (
                mock.patch.object(config, "PROJECT_ROOT", root),
                mock.patch.object(config, "RUNTIME_DIR", root / "runtime"),
                mock.patch.dict(os.environ, {"INBOX_DB_PATH": ""}),
            ):
                self.assertEqual(config.runtime_path("INBOX_DB_PATH", "inbox.sqlite3"), legacy)
                self.assertEqual(legacy.read_bytes(), b"existing database")

    def test_new_database_uses_runtime_and_explicit_override_has_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                mock.patch.object(config, "PROJECT_ROOT", root),
                mock.patch.object(config, "RUNTIME_DIR", root / "runtime"),
                mock.patch.dict(os.environ, {"INBOX_DB_PATH": ""}),
            ):
                self.assertEqual(
                    config.runtime_path("INBOX_DB_PATH", "inbox.sqlite3"),
                    root / "runtime/inbox.sqlite3",
                )
                (root / "inbox.sqlite3").touch()
                with mock.patch.dict(os.environ, {"INBOX_DB_PATH": "custom/inbox.sqlite3"}):
                    self.assertEqual(
                        config.runtime_path("INBOX_DB_PATH", "inbox.sqlite3"),
                        root / "custom/inbox.sqlite3",
                    )

    def test_fresh_process_imports_api_and_cli_from_other_directory_without_creating_databases(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            env = os.environ.copy()
            env.update(
                {"PYTHONPATH": str(config.PROJECT_ROOT), "RUNTIME_DIR": str(temp / "runtime")}
            )
            for variable, filename in [
                ("INBOX_DB_PATH", "inbox.sqlite3"),
                ("METRICS_DB_PATH", "metrics.sqlite3"),
                ("RAG_DB_PATH", "rag.sqlite3"),
            ]:
                env[variable] = str(temp / "runtime" / filename)
            command = "from app.main import app, public_app; from app.core import config; import json; print(json.dumps(str(config.INBOX_DB_PATH)))"
            result = subprocess.run(
                [sys.executable, "-c", command],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            self.assertEqual(json.loads(result.stdout), str(temp / "runtime/inbox.sqlite3"))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.cli.cost_report",
                    "--period",
                    "2026-10",
                    "--db",
                    str(temp / "absent.sqlite3"),
                ],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            self.assertIn("sin datos", result.stdout)
            self.assertFalse((temp / "runtime").exists())
            self.assertFalse((temp / "absent.sqlite3").exists())
