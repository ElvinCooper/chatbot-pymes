"""Guardas de artefactos SDD y límites entre evidencia y autorización."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import sdd


class SddTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(sdd.PROJECT_ROOT / ".specify/templates", self.root / ".specify/templates")
        self.name = "001-prueba"
        self.path = sdd.create_feature(self.root, self.name, "Función de prueba")

    def metadata(self, **updates):
        path = self.path / "feature.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(updates)
        path.write_text(json.dumps(data), encoding="utf-8")

    def authorize_fixture(self):
        # Datos ficticios: se prueba el registro, nunca consentimiento real.
        self.metadata(
            state="approved",
            authorization={
                "revision": 1,
                "by": "persona ficticia",
                "reference": "decisión simulada para test",
                "scope": "T001-T003",
            },
        )
        for filename in ("spec.md", "plan.md", "tasks.md"):
            path = self.path / filename
            text = (
                path.read_text(encoding="utf-8")
                .replace("TODO", "Descripción completa")
                .replace("Pendiente", "Decisión simulada")
            )
            path.write_text(text, encoding="utf-8")

    def test_new_creates_only_draft_artifacts_and_does_not_self_authorize(self):
        self.assertEqual({p.name for p in self.path.iterdir()}, {"feature.json", *sdd.DOCUMENTS})
        data = json.loads((self.path / "feature.json").read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "draft")
        self.assertIsNone(data["authorization"])
        self.assertFalse(sdd.check_feature(self.root, self.name))
        self.assertTrue(sdd.check_feature(self.root, self.name, ready=True))

    def test_traversal_duplicate_number_and_overwrite_are_rejected(self):
        for name in ["../escape", "001-../escape", self.name, "001-otra"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                sdd.create_feature(self.root, name, "Otro título")
        self.assertEqual(len(list((self.root / "specs").iterdir())), 1)

    def test_missing_template_does_not_leave_partial_feature(self):
        (self.root / ".specify/templates/plan-template.md").unlink()
        with self.assertRaises(FileNotFoundError):
            sdd.create_feature(self.root, "002-nueva", "Nueva")
        self.assertFalse((self.root / "specs/002-nueva").exists())

    def test_ready_requires_current_revision_and_complete_documents(self):
        self.metadata(state="approved")
        self.assertTrue(sdd.check_feature(self.root, self.name, ready=True))
        self.authorize_fixture()
        path = self.path / "plan.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nTodo el proceso usa stdlib.\n", encoding="utf-8"
        )
        self.assertFalse(sdd.check_feature(self.root, self.name, ready=True))
        self.metadata(revision=2)
        self.assertTrue(sdd.check_feature(self.root, self.name, ready=True))

    def test_unknown_reference_and_uncovered_criterion_are_detected(self):
        path = self.path / "tasks.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("AC-001", "AC-999"), encoding="utf-8"
        )
        errors = sdd.check_feature(self.root, self.name)
        self.assertTrue(any("desconocidas" in error for error in errors))
        self.assertTrue(any("sin tarea" in error for error in errors))

    def test_missing_document_and_duplicate_requirement_are_detected(self):
        path = self.path / "spec.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n- **FR-001**: Repetido.\n", encoding="utf-8"
        )
        self.assertTrue(any("duplicados" in e for e in sdd.check_feature(self.root, self.name)))
        (self.path / "validation.md").unlink()
        self.assertTrue(sdd.check_feature(self.root, self.name))

    def test_implemented_requires_completed_tasks(self):
        self.authorize_fixture()
        self.metadata(state="implemented")
        self.assertTrue(any("sin completar" in e for e in sdd.check_feature(self.root, self.name)))
        path = self.path / "tasks.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("- [ ]", "- [x]"), encoding="utf-8"
        )
        self.assertFalse(sdd.check_feature(self.root, self.name))

    def test_verified_requires_technical_evidence_but_not_human_acceptance(self):
        self.authorize_fixture()
        path = self.path / "tasks.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("- [ ]", "- [x]"), encoding="utf-8"
        )
        self.metadata(state="verified")
        self.assertTrue(sdd.check_feature(self.root, self.name))
        path = self.path / "validation.md"
        text = path.read_text(encoding="utf-8")
        technical, acceptance = text.split("## Aceptación humana", 1)
        technical = technical.replace("TODO", "Procedimiento documentado").replace(
            "Pendiente", "PASS simulado"
        )
        path.write_text(technical + "## Aceptación humana" + acceptance, encoding="utf-8")
        self.assertFalse(sdd.check_feature(self.root, self.name))
        self.metadata(state="accepted")
        self.assertTrue(any("aceptación" in e for e in sdd.check_feature(self.root, self.name)))

    def test_malformed_metadata_fails_and_cli_requires_named_ready_target(self):
        (self.path / "feature.json").write_text("[]", encoding="utf-8")
        self.assertTrue(sdd.check_feature(self.root, self.name))
        with mock.patch.object(sdd, "PROJECT_ROOT", self.root):
            self.assertEqual(sdd.main(["check", "--ready"]), 1)
            self.assertEqual(sdd.main(["check"]), 1)
