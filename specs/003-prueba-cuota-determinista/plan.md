# Plan — 003-prueba-cuota-determinista

## Requisitos cubiertos

FR-001, FR-002, NFR-001; AC-001, AC-002, AC-003.

## Diseño y archivos afectados

tests/api/test_security.py importa quotas y reemplaza quotas.time dentro del test de cuota por un mock a tiempo fijo. Conservar requests reales a TestClient, SQLite temporal y clientes mockeados; comprobar Retry-After y que la petición rechazada no llama al LLM.

tests/persistence/test_inbox.py agrega prueba de ventana: agotar 20, rechazar en t=1259.999 y consumir otras 20 desde t=1260, volviendo a rechazar la 21. La prueba comprueba política real, no reimplementa SQL. Fijar también el reloj de las tres reservas del test compartido de cuota/sesión, manteniendo el reloj real para su comprobación de TTL.

## Contratos y datos

Sin cambios de API, cuota de 20/minuto, reserva SQLite, esquemas, dependencias ni workflow. Bases temporales, sin datos reales.

## Plan de pruebas

AC-001: test HTTP con tiempo fijo y contador de llamadas. AC-002: nuevo test de frontera sin sleeps. AC-003: reproducción de fallo con reloj que cambia antes de la petición 21; ejecutar pruebas focalizadas y suite completa, Ruff, compileall, checker y diff check. Registrar resultados y separar CI remoto.

## Migración y rollback

No aplica migración de datos. Revertir los cambios de tests y artefactos restaura el estado anterior sin modificar producción.

## Revisión de la constitución

Se conserva límite de seguridad y prueba del rechazo; no se oculta el fallo con skips ni se debilita el criterio. Mock solo de referencia temporal de cuotas; ninguna espera dependiente del reloj real.
