# Validación — 002-pruebas-rutas-windows

## Matriz de aceptación

| Criterio | Requisitos | Prueba o procedimiento reproducible | Resultado | Evidencia |
|---|---|---|---|---|
| AC-001 | FR-001 | test_explicit_database_path_resolves_windows_short_directory_alias | PASS con alias real; sin skip | GetShortPathNameW y samefile sobre carpeta existente; archivo destino no creado |
| AC-002 | FR-001 | Tests de override explícito y proceso/CLI desde otro cwd | PASS | Antes: 2 fallos de 4 tests con TEMP 8.3; después: 5/5 y suite completa |
| AC-003 | NFR-001 | unittest discover, Ruff, compileall y checker SDD | PASS local | 74 pruebas en Windows/Python 3.13.11 con TEMP/TMP en alias 8.3; sin cambios runtime/locks |

## Comandos y entorno

2026-10-14, Windows, Python 3.13.11. TEMP y TMP apuntaron a la forma 8.3 de una carpeta temporal permitida del workspace. No se utilizó la carpeta de datos real del negocio.

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m compileall -q tests\core\test_config.py
.venv\Scripts\python.exe scripts\sdd.py check
git diff --check
```

La reproducción previa produjo exactamente los dos fallos reportados. Tras normalizar las aserciones con Path.resolve y añadir la regresión, los cinco tests de rutas y las 74 pruebas de la suite pasaron. Ruff, compilación, checker y diff sin errores.

Las primeras ejecuciones completas se interrumpieron por bloqueo de la creación del loop asyncio en socket._fallback_socketpair/socket.accept, diagnosticado con faulthandler. Se solicitó permiso de red para la conexión interna de loopback en el sandbox; después la suite se completó. Clientes de proveedores/canales permanecieron simulados.

## Riesgos y verificaciones pendientes

No se ejecutó Linux/CI remoto. El test 8.3 se omite explícitamente fuera de Windows o si el volumen no ofrece un alias distinto; en esta ejecución Windows sí se ejercitó. Las otras aserciones de rutas siguen siendo multiplataforma. Estado implemented para conservar estas verificaciones remotas separadas de los resultados locales.

## Revisión de alcance

Solo tests/core/test_config.py y artefactos de esta corrección. Sin modificar app/core/config.py ni los chequeos de no creación de bases. Se explicó el motivo de unittest; no se instaló ni migró a pytest.

## Aceptación humana

Pendiente. No se ha registrado aceptación del resultado ni realizado commit/push.
