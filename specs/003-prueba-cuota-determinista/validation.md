# Validación — 003-prueba-cuota-determinista

## Matriz de aceptación

| Criterio | Requisitos | Prueba o procedimiento reproducible | Resultado | Evidencia |
|---|---|---|---|---|
| AC-001 | FR-001 | test_chat_message_limit_and_rate_limit | PASS | 422 por longitud, 20 respuestas 200, 21ª 429 con Retry-After=60 y solo 20 llamadas LLM |
| AC-002 | FR-002 | test_quota_remains_exhausted_until_next_fixed_window | PASS | Agotar 20 en t=1200; rechazar en 1259.999; permitir otras 20 desde 1260 y rechazar exceso |
| AC-003 | NFR-001 | Reproducción, pruebas focalizadas, suite y controles | PASS local | 75 tests aprobados en Windows; Ruff, compileall, checker y diff check correctos |

## Comandos y entorno

Windows, Python 3.13.11, .venv local. TEMP/TMP dentro del workspace permitido del sandbox.

```powershell
.venv\Scripts\python.exe -m unittest tests.api.test_security.SecurityTest.test_chat_message_limit_and_rate_limit tests.persistence.test_inbox.InboxTest.test_quota_remains_exhausted_until_next_fixed_window tests.persistence.test_inbox.InboxTest.test_shared_quota_and_session_expiry -v
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m compileall -q tests/api/test_security.py tests/persistence/test_inbox.py
.venv\Scripts\python.exe scripts\sdd.py check
git diff --check
```

La reproducción aislada del test original controló solo quotas.time: primeras 20 solicitudes en t=1200 y petición 21 en t=1260. Produjo exactamente AssertionError: 200 != 429. Tras corregir, las tres pruebas focalizadas y la suite de 75 pasaron. Los mensajes ERROR SDD del final del log son salidas esperadas de tests negativos del checker.

## Riesgos y verificaciones pendientes

No se ejecutó una nueva corrida de GitHub Actions ni Linux. El log original no contiene timestamps por petición; demuestra el fallo reportado, y el cruce de ventana se confirmó como mecanismo reproducible por código y simulación controlada. No afirmar que se observaron directamente los tiempos del runner remoto.

## Revisión de alcance

Solo tests/api/test_security.py, tests/persistence/test_inbox.py y esta spec. Sin cambios de app/, locks, workflow, límite de 20/minuto ni lógica SQL. El mock reemplaza el atributo time de quotas y no el módulo global del reloj; asyncio/sesiones conservan su tiempo real. Se estabilizó también el test existente de cuota compartida para evitar la misma carrera.

## Aceptación humana

Pendiente de revisión y nueva ejecución remota. Estado implemented; no se hizo commit ni push de esta corrección durante la validación local.
