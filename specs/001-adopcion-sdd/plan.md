# Plan — 001-adopcion-sdd

## Requisitos cubiertos

FR-001, FR-002, FR-003, FR-004, NFR-001; AC-001 a AC-005.

## Diseño y archivos afectados

`.specify/memory/constitution.md` y cuatro plantillas Markdown; `specs/README.md`, baseline y primera spec; `docs/SDD.md`, enlaces en README/ESTRUCTURA y reglas AGENTS. `scripts/sdd.py` tiene new/check, sin comandos de autoaprobación. Tests en `tests/core/test_sdd.py`. CI usa check y plantilla PR enlaza artefactos/evidencia.

## Contratos y datos

feature.json conserva id, revision positiva, state y authorization/acceptance opcionales. La autorización incluye revision/by/reference/scope; la aceptación incluye revision/by/reference. Estados draft/approved/implemented/verified/accepted. Referencias FR/NFR/AC y tareas T en Markdown con formato verificable. Todo es documentación del desarrollo; no cambia modelos/API ni SQLite.

## Plan de pruebas

AC-001: scaffold y draft sin autorización. AC-002: traversal, número repetido, carpeta existente y falta de plantilla. AC-003: revisión obsoleta, referencias desconocidas, faltantes/duplicados y tareas incompletas. AC-004: validación técnica y aceptación separadas. AC-005: revisar paso CI, ejecutar checker, unittest discover y Ruff. Registrar comandos/resultados reales en validation.md; no declarar CI Linux pasado sin una ejecución real.

## Migración y rollback

No aplica migración de datos: cambio aditivo de herramientas/documentación. Rollback del proceso retiraría nuevos artefactos y el paso CI y restauraría guías/AGENTS de la revisión anterior; conservar specs como historial. No afecta las bases ni los locks.

## Revisión de la constitución

No altera auth, datos del negocio, costes, proveedores, índices o canales. Solo stdlib para scaffold/checker. Autorización deriva de la solicitud explícita del usuario; aceptación no se autoasigna. Mantener el alcance a la preparación SDD y comunicar límites semánticos/plataformas.
