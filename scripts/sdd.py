"""Crear y verificar artefactos SDD locales; no otorga autorizaciones."""

import argparse
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"[0-9]{3}-[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
STATES = {"draft", "approved", "implemented", "verified", "accepted"}
DOCUMENTS = {
    "spec.md": [
        "Problema y resultado esperado",
        "Alcance",
        "Fuera de alcance",
        "Requisitos",
        "Criterios de aceptación",
        "Restricciones y riesgos",
        "Preguntas y decisiones",
        "Revisiones",
    ],
    "plan.md": [
        "Requisitos cubiertos",
        "Diseño y archivos afectados",
        "Contratos y datos",
        "Plan de pruebas",
        "Migración y rollback",
        "Revisión de la constitución",
    ],
    "tasks.md": ["Desviaciones"],
    "validation.md": [
        "Matriz de aceptación",
        "Comandos y entorno",
        "Riesgos y verificaciones pendientes",
        "Revisión de alcance",
        "Aceptación humana",
    ],
}
ID = re.compile(r"\b(?:FR|NFR|AC)-[0-9]{3}\b")
DEFINITION = re.compile(r"^- \*\*((?:FR|NFR|AC)-[0-9]{3})\*\*(.*)$", re.MULTILINE)
TASK = re.compile(r"^- \[([ xX])\] (T[0-9]{3}) \[([^\]]+)\] (.+)$", re.MULTILINE)
PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|(?i:PENDIENTE))\b")


def feature_path(root: Path, name: str) -> Path:
    if not NAME.fullmatch(name):
        raise ValueError("Nombre inválido: usar NNN-nombre-en-kebab-case")
    specs = (root / "specs").resolve()
    if not specs.is_relative_to(root.resolve()):
        raise ValueError("specs debe estar dentro del proyecto")
    path = specs / name
    if path.is_symlink() or not path.resolve().is_relative_to(specs):
        raise ValueError("La feature debe estar dentro de specs, sin enlaces simbólicos")
    return path


def create_feature(root: Path, name: str, title: str) -> Path:
    path = feature_path(root, name)
    if not title.strip() or "\n" in title or "\r" in title:
        raise ValueError("El título debe ser una línea no vacía")
    if path.exists():
        raise ValueError("La carpeta ya existe; no se sobrescriben especificaciones")
    number = name.split("-", 1)[0]
    if path.parent.exists() and any(
        p.is_dir() and p.name.startswith(number + "-") for p in path.parent.iterdir()
    ):
        raise ValueError("El número de especificación ya está utilizado")
    content = {}
    for filename in DOCUMENTS:
        template = root / ".specify/templates" / filename.replace(".md", "-template.md")
        content[filename] = (
            template.read_text(encoding="utf-8")
            .replace("{{ID}}", name)
            .replace("{{TITLE}}", title.strip())
        )
    metadata = {
        "id": name,
        "revision": 1,
        "state": "draft",
        "authorization": None,
        "acceptance": None,
    }
    content["feature.json"] = json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
    path.mkdir(parents=True, exist_ok=False)
    for filename, text in content.items():
        with (path / filename).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    return path


def decision_valid(value, revision: int, *, authorization: bool = False) -> bool:
    fields = ["by", "reference"] + (["scope"] if authorization else [])
    return (
        isinstance(value, dict)
        and type(value.get("revision")) is int
        and value["revision"] == revision
        and all(
            isinstance(value.get(key), str)
            and value[key].strip()
            and not PLACEHOLDER.search(value[key])
            for key in fields
        )
    )


def check_feature(root: Path, name: str, *, ready: bool = False) -> list[str]:
    path = feature_path(root, name)
    errors = []
    texts = {}
    for filename in ["feature.json", *DOCUMENTS]:
        file = path / filename
        if file.is_symlink() or not file.is_file():
            errors.append(f"{filename}: archivo requerido ausente o enlace simbólico")
            continue
        texts[filename] = file.read_text(encoding="utf-8")
    if errors:
        return errors
    try:
        metadata = json.loads(texts.pop("feature.json"))
    except json.JSONDecodeError:
        return ["feature.json: JSON inválido"]
    if not isinstance(metadata, dict):
        return ["feature.json debe ser un objeto"]
    state, revision = metadata.get("state"), metadata.get("revision")
    if metadata.get("id") != name or type(revision) is not int or revision < 1:
        errors.append("feature.json: id incorrecto o revisión inválida")
    if not isinstance(state, str) or state not in STATES:
        errors.append("feature.json: estado inválido")
        return errors
    authorized = state != "draft"
    if ready and not authorized:
        errors.append("No está autorizada para implementación: estado draft")
    if authorized and not decision_valid(
        metadata.get("authorization"), revision, authorization=True
    ):
        errors.append(
            "Falta referencia humana de autorización con alcance para la revisión vigente"
        )
    if state == "accepted" and not decision_valid(metadata.get("acceptance"), revision):
        errors.append("Falta referencia humana de aceptación para la revisión vigente")
    for filename, sections in DOCUMENTS.items():
        for heading in sections:
            if not re.search(r"^## " + re.escape(heading) + r"\s*$", texts[filename], re.MULTILINE):
                errors.append(f"{filename}: falta sección {heading}")
        if authorized and filename != "validation.md" and PLACEHOLDER.search(texts[filename]):
            errors.append(f"{filename}: quedan marcadores TODO/TBD/Pendiente")
    definitions = DEFINITION.findall(texts["spec.md"])
    ids = [item[0] for item in definitions]
    known = set(ids)
    requirements = {item for item in known if not item.startswith("AC-")}
    criteria = known - requirements
    if len(ids) != len(known):
        errors.append("spec.md: IDs de requisitos/criterios duplicados")
    if not requirements or not criteria:
        errors.append("spec.md: requiere al menos un requisito FR/NFR y un criterio AC")
    criterion_refs = set()
    for identifier, body in definitions:
        if identifier.startswith("AC-"):
            refs = set(ID.findall(body)) & requirements
            criterion_refs |= refs
            if not refs:
                errors.append(f"spec.md: {identifier} no refiere ningún requisito")
    if requirements - criterion_refs:
        errors.append(
            "spec.md: requisitos sin criterio de aceptación: "
            + ", ".join(sorted(requirements - criterion_refs))
        )
    for filename, text in texts.items():
        unknown = set(ID.findall(text)) - known
        if unknown:
            errors.append(f"{filename}: referencias desconocidas: {', '.join(sorted(unknown))}")
    tasks = TASK.findall(texts["tasks.md"])
    if not tasks:
        errors.append("tasks.md: faltan tareas con formato - [ ] T001 [FR-001, AC-001] descripción")
    if len({task[1] for task in tasks}) != len(tasks):
        errors.append("tasks.md: IDs de tareas duplicados")
    task_refs = set()
    for checked, identifier, refs_text, _ in tasks:
        refs = set(ID.findall(refs_text))
        task_refs |= refs
        if not refs:
            errors.append(f"tasks.md: {identifier} sin referencias FR/NFR/AC")
        if state in {"implemented", "verified", "accepted"} and checked.lower() != "x":
            errors.append(f"tasks.md: {identifier} sigue sin completar")
    if known - task_refs:
        errors.append(
            "tasks.md: requisitos/criterios sin tarea: " + ", ".join(sorted(known - task_refs))
        )
    plan_refs = set(ID.findall(texts["plan.md"]))
    if requirements - plan_refs:
        errors.append("plan.md: requisitos sin referencia en el plan")
    rows = re.findall(r"^\|\s*(AC-[0-9]{3})\s*\|(.+)$", texts["validation.md"], re.MULTILINE)
    if criteria - {row[0] for row in rows}:
        errors.append("validation.md: criterios sin fila en la matriz de aceptación")
    if len(rows) != len({row[0] for row in rows}):
        errors.append("validation.md: criterios duplicados en la matriz")
    technical_validation = texts["validation.md"].split("## Aceptación humana", 1)[0]
    if state in {"verified", "accepted"} and PLACEHOLDER.search(technical_validation):
        errors.append("validation.md: quedan resultados o evidencias pendientes")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="Crear borrador sin implementar ni aprobar")
    new.add_argument("name")
    new.add_argument("--title", required=True)
    check = commands.add_parser("check", help="Verificar estructura y referencias de las specs")
    check.add_argument("--feature")
    check.add_argument(
        "--ready",
        action="store_true",
        help="Exigir autorización registrada y documentos sin marcadores",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "new":
            path = create_feature(PROJECT_ROOT, args.name, args.title)
            print(f"Borrador creado: {path.relative_to(PROJECT_ROOT)}. No autoriza implementación.")
            return 0
        if args.ready and not args.feature:
            raise ValueError("--ready requiere --feature")
        specs = PROJECT_ROOT / "specs"
        if not specs.is_dir():
            raise ValueError("Falta la carpeta specs")
        names = (
            [args.feature]
            if args.feature
            else sorted(p.name for p in specs.iterdir() if p.is_dir())
        )
        failed = False
        for name in names:
            errors = check_feature(PROJECT_ROOT, name, ready=args.ready)
            failed |= bool(errors)
            for error in errors:
                print(f"ERROR {name}: {error}")
        if not failed:
            print(
                f"SDD: estructura y referencias válidas ({len(names)} especificaciones). No verifica consentimiento ni semántica."
            )
        return int(failed)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"ERROR SDD: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
