# Especificaciones

El flujo está en [docs/SDD.md](../docs/SDD.md). La constitución vive en [.specify/memory/constitution.md](../.specify/memory/constitution.md).

Cada cambio de comportamiento usa `NNN-nombre-en-kebab-case/` con:

```text
feature.json    # Estado, revisión y referencias de autorización/aceptación
spec.md         # Qué y por qué: requisitos, exclusiones, aceptación
plan.md         # Cómo: archivos, contratos, pruebas y rollback
tasks.md        # Trabajo concreto trazable a requisitos
validation.md   # Resultados observados y verificaciones pendientes
```

Añadir `research.md`, `data-model.md`, `contracts/` o `checklists/` solo cuando el cambio lo necesite. No duplicar contratos Pydantic completos sin necesidad; enlazar la fuente.

Crear: `uv run --no-project python scripts/sdd.py new 001-nombre --title "Descripción breve"`.
Comprobar todos: `uv run --no-project python scripts/sdd.py check`.
Comprobar preparación para implementar: `uv run --no-project python scripts/sdd.py check --feature 001-nombre --ready`.

No hay una función de aprobación automática. Las referencias en feature.json se rellenan únicamente con decisiones humanas auténticas; JSON no prueba por sí mismo identidad o consentimiento. CI revisa estructura, y el revisor verifica contenido/decisiones.

[baseline.md](baseline.md) describe el punto de partida, no un backlog autorizado. Los planes históricos de docs tampoco autorizan funcionalidades nuevas.
