# Validación — 001-adopcion-sdd

## Matriz de aceptación

| Criterio | Requisitos | Prueba o procedimiento reproducible | Resultado | Evidencia |
|---|---|---|---|---|
| AC-001 | FR-001, FR-002 | test_new_creates_only_draft_artifacts_and_does_not_self_authorize | PASS | tests/core/test_sdd.py; suite de 73 pruebas |
| AC-002 | FR-002 | test_traversal_duplicate_number_and_overwrite_are_rejected y test_missing_template_does_not_leave_partial_feature | PASS | Suite unittest local |
| AC-003 | FR-003 | Tests de revisión vigente, referencias desconocidas, documentos/IDs faltantes/duplicados y tareas incompletas | PASS | Nueve pruebas SDD en la suite completa |
| AC-004 | FR-003 | test_verified_requires_technical_evidence_but_not_human_acceptance | PASS | verified permite aceptación humana por resolver; accepted exige referencia |
| AC-005 | FR-004, NFR-001 | Suite/checker/Ruff locales y revisión del paso CI en matriz Ubuntu/Windows | PASS local; ejecución remota por realizar | Workflow security.yml contiene python scripts/sdd.py check; no se ejecutó CI remoto |

## Comandos y entorno

2026-10-14, Windows, Python 3.13.11, entorno .venv existente:

```powershell
.venv\Scripts\python.exe scripts\sdd.py check --feature 001-adopcion-sdd --ready
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m compileall -q scripts tests
```

Checker: correcto. Unittest: 73 pruebas, 78.028 segundos, OK. Ruff y compileall: correctos. git diff --check: correcto. Gitleaks dir sobre el proyecto: sin hallazgos. No se cambiaron dependencias ni se enviaron peticiones a proveedores del chatbot. Los mensajes ERROR SDD que imprime la suite proceden de casos negativos controlados que comprueban el rechazo de entradas inválidas.

El entorno temporal se redirigió a una carpeta de trabajo permitida para ejecutar los tests dentro del sandbox local; no es un requisito del proyecto en un servidor normal.

## Riesgos y verificaciones pendientes

Ejecutar CI remoto y la suite en Linux. La revisión del YAML demuestra configuración, no una ejecución exitosa de GitHub Actions. Por eso la spec permanece en implemented y no en verified/accepted. El checker no autentica consentimiento, evalúa calidad semántica ni verifica que un resultado declarado corresponda a una prueba real; el revisor contrasta documentos, diff y evidencia.

## Revisión de alcance

El diff contiene únicamente artefactos SDD, script/stdlib, tests, enlaces/reglas AGENTS y CI/PR. El código del backend, los contratos, las bases y los locks no cambian. La solicitud humana autoriza preparar el flujo; no se registró aceptación del resultado ni permiso de push/despliegue.

## Aceptación humana

Pendiente. acceptance permanece null. La solicitud original autorizó preparación; el usuario conserva la decisión de aceptar el resultado.
