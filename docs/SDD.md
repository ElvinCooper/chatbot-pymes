# Desarrollo guiado por especificaciones

Este repositorio adopta un flujo local de SDD inspirado en [GitHub Spec Kit](https://github.github.com/spec-kit/index.html): especificar, planificar, dividir en tareas, implementar y contrastar el resultado con la especificación. La separación entre qué se quiere y cómo se implementa sigue su [referencia del proceso](https://github.github.com/spec-kit/reference/agentic-sdd.html). Son plantillas y herramientas propias del repositorio; no se instaló Spec Kit ni sus comandos slash.

## Estructura

```text
.specify/
  memory/constitution.md
  templates/{spec,plan,tasks,validation}-template.md
specs/
  README.md
  baseline.md
  NNN-feature/
    feature.json
    spec.md
    plan.md
    tasks.md
    validation.md
scripts/sdd.py
.github/PULL_REQUEST_TEMPLATE.md
```

La constitución fija invariantes del proyecto. Cada feature mantiene requisitos FR/NFR, criterios AC y tareas T. La validación enlaza los criterios con pruebas o procedimientos. Documentos de investigación y contratos son opcionales.

## Flujo diario

1. **Preparar**. La IA lee AGENTS.md, constitución y baseline; identifica el comportamiento actual en código/tests. Crea borradores de spec, plan y tareas. Resuelve lo que pueda con evidencia y presenta dudas materiales al usuario. No cambia todavía el backend por una idea nueva.
2. **Revisar y autorizar**. El usuario revisa el alcance, exclusiones, criterios y plan concreto. Puede autorizar todo o un conjunto de tareas en un único mensaje. Registrar la revisión y referencia auténtica en feature.json; no solicitar otra aprobación para pasos cubiertos por esa autorización.
3. **Implementar**. Ejecutar tareas autorizadas y pruebas pertinentes. Completar TODO de spec/plan/tasks antes del gate `--ready`. Mantener los borradores de validación como pendientes hasta ejecutar los controles. Una ampliación material crea nueva revisión y vuelve a autorización; decisiones técnicas rutinarias dentro del alcance continúan.
4. **Verificar**. Vincular cada AC con resultado y evidencia en validation.md, ejecutar suite/lint y controles afectados, actualizar tareas. Estado `verified` significa evidencia técnica completa, no aceptación humana. Un fallo o plataforma sin comprobar se comunica; no inventar resultados.
5. **Aceptar**. Presentar diff, resultados y límites al usuario. La aceptación auténtica se registra; commit, push y despliegue siguen la autorización de la conversación. Conservar archivos para futuras modificaciones.

Estados: `draft → approved → implemented → verified → accepted`. Pueden volver a `draft` al cambiar alcance/revisión. `implemented` exige tareas completas; `verified` además exige validación sin pendientes. `accepted` exige referencia humana de aceptación para esa misma revisión.

## Comandos

Desde la raíz, con el entorno instalado:

```bash
uv run --no-project python scripts/sdd.py new 001-exportar-informe --title "Exportar informe de costes"
uv run --no-project python scripts/sdd.py check
uv run --no-project python scripts/sdd.py check --feature 001-exportar-informe --ready
uv run --no-project python -m unittest discover -s tests -v
uv run --no-project python -m ruff check .
```

`new` solo crea artefactos, no cambia código, ramas ni estados de aprobación. Rechaza nombres inválidos, IDs repetidos y carpetas existentes. `check` valida archivos, metadatos e IDs de requisitos/criterios/tareas; los borradores pueden contener TODO. `--ready` exige estado autorizado, referencia de la revisión vigente y documentos de implementación sin TODO. La verificación automática puede detectar referencias rotas y documentación incompleta; no demuestra que el diseño sea correcto, que las pruebas prueben el requisito o que una aprobación sea auténtica. El revisor comprueba eso con el diff y la conversación/PR.

No se impuso un bloqueo de merge en GitHub: la plantilla PR y CI aportan revisión/evidencia, y la protección de ramas/revisores depende de la configuración del repositorio remoto.

## Ejemplo de autorización registrada

Tras un mensaje humano auténtico, rellenar el objeto `authorization` (el siguiente es ilustrativo, no una aprobación real):

```json
{"revision": 1, "by": "responsable del proyecto", "reference": "URL del comentario o referencia al mensaje humano", "scope": "T001-T003 según spec y plan revisión 1"}
```

El objeto `acceptance` usa revision/by/reference. Nunca copiar esta referencia de ejemplo como evidencia. Un comentario de otro agente no es consentimiento humano. Si la autorización limita tareas, la IA respeta ese subconjunto aunque el estado global sea approved.

## Cambios pequeños y correcciones urgentes

Un cambio editorial sin comportamiento puede usar solo descripción y comprobación proporcional en el PR. Bugfixes y refactors que afectan ejecución usan una spec breve con reproducción, resultado esperado y regresión. El usuario puede autorizar un arreglo urgente directamente: registrar alcance/decisión y preparar la spec breve antes de editar, sin exigir un ciclo de aprobación redundante. No eximir silenciosamente seguridad, migraciones, costes, dependencias o contratos.

## Prompts que puedes usar

- «Prepara la spec de X; incluye exclusiones, riesgos y criterios. No implementes todavía».
- «Revisa spec/plan/tasks de 001-feature y señala inconsistencias».
- «Autorizo la revisión 1 de 001-feature, tareas T001 a T003. Implementa y ejecuta sus pruebas».
- «Contrasta el diff con la spec; presenta evidencia por criterio y pendientes».

Este flujo controla alcance y deja trazabilidad; AGENTS.md es una instrucción para agentes, no un mecanismo de aislamiento o una garantía de obediencia.
