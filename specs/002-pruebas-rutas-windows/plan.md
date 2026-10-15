# Plan — 002-pruebas-rutas-windows

## Requisitos cubiertos

FR-001, NFR-001; AC-001, AC-002, AC-003.

## Diseño y archivos afectados

En tests/core/test_config.py normalizar el resultado JSON y la ruta esperada del proceso nuevo; normalizar la ruta esperada del override explícito. Agregar prueba Windows con GetShortPathNameW sobre carpeta temporal, no sobre datos reales. Comprobar también que una ruta distinta no compare igual.

## Contratos y datos

No cambia producción, API, esquemas, SQLite ni locks. Solo tests y artefactos SDD.

## Plan de pruebas

AC-001: regresión con alias Windows real, comparación de otra ruta. AC-002: tests originales con aserciones canónicas y no creación de archivos. AC-003: unittest discover, Ruff y checker SDD. Mantener TEMP dentro del workspace del sandbox; la prueba de alias convierte esa carpeta con la API Windows.

## Migración y rollback

No aplica migración; restaurar las aserciones/tests de esta revisión revierte el cambio sin tocar datos.

## Revisión de la constitución

Respeta unittest y tests sin servicios externos. Usa ctypes de stdlib solo en Windows. No agrega dependencias ni cambia runtime.
