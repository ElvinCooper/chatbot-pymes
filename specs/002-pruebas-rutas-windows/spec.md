# 002-pruebas-rutas-windows — Comparar rutas equivalentes

## Problema y resultado esperado

El usuario ejecuta 73 tests manualmente y dos fallan porque TEMP usa un alias 8.3 y config.resolve_path devuelve el nombre completo. Son dos representaciones de la misma ubicación. Las aserciones deben verificar ubicación y las garantías de no crear bases.

## Alcance

Corregir las dos aserciones y agregar una regresión de alias Windows real cuando el sistema los permita.

## Fuera de alcance

Cambiar app/core/config.py, migrar a pytest, instalar dependencias o modificar servicios/datos.

## Requisitos

- **FR-001**: Comparar ubicaciones canónicas para rutas normalizadas sin debilitar el chequeo de ausencia de bases.
- **NFR-001**: Conservar suite Windows/Linux y locks existentes; ejecutar regresión 8.3 en Windows si está disponible.

## Criterios de aceptación

- **AC-001** [FR-001]: Rutas con alias corto y nombre largo comparan iguales tras normalización; otra ubicación sigue siendo distinta.
- **AC-002** [FR-001]: El proceso nuevo y CLI importan desde otro directorio sin crear bases y la opción explícita conserva prioridad.
- **AC-003** [NFR-001]: Suite y Ruff pasan; si no se puede generar alias 8.3 se comunica el skip. No se cambia runtime ni dependencias.

## Restricciones y riesgos

No usar samefile sobre bases inexistentes; normalizar con Path.resolve, manteniendo las aserciones de no existencia. No simular que se probó Linux.

## Preguntas y decisiones

El reporte implica corregir el fallo localizado. Unittest era la suite/CI existente; pytest no se añadió durante la reorganización. La pregunta sobre pytest no autoriza una migración.

## Revisiones

| Revisión | Cambio | Referencia de la decisión humana |
|---|---|---|
| 1 | Corregir falso negativo de rutas equivalentes | Reporte del usuario en esta conversación |
