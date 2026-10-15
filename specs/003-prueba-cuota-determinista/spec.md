# 003-prueba-cuota-determinista — Pruebas de cuota estables

## Problema y resultado esperado

El workflow Windows reporta 200 en la petición que el test esperaba rechazada con 429. El test usa reloj real mientras la cuota utiliza int(time.time()) // 60. Si las peticiones cruzan un minuto, pertenecen a dos ventanas y la última puede aceptarse. La prueba debe controlar su tiempo sin cambiar la política de producción.

## Alcance

Reproducción controlada del cambio de ventana; reloj exclusivo del módulo de cuotas en el test API; regresión del límite y reinicio de ventana.

## Fuera de alcance

Cambiar producción, elevar cuotas, usar esperas reales, cambiar a ventana deslizante, migrar framework, modificar workflow o dependencias y publicar cambios.

## Requisitos

- **FR-001**: Verificar 20 respuestas 200 y rechazo 429 de la petición 21 en la misma ventana fija controlada.
- **FR-002**: Verificar que la cuota sigue agotada justo antes del límite y vuelve a permitir solicitudes al comenzar el minuto siguiente.
- **NFR-001**: El reloj simulado queda limitado a cuotas; el reloj global de asyncio/sesiones no se modifica y la prueba no depende de duración/velocidad del runner.

## Criterios de aceptación

- **AC-001** [FR-001]: Con reloj fijo se mantienen límite de longitud 422 y límite de cuota 429 sin más llamadas LLM al agotar.
- **AC-002** [FR-002]: Después de consumir 20 reservas, t=1259.999 sigue rechazando y t=1260 permite solicitudes de la nueva ventana con su propio límite.
- **AC-003** [NFR-001]: La prueba original falla de forma controlada al pasar de ventana antes de la petición 21; tras corregir, suite y Ruff/checker pasan sin cambios en app/ ni locks.

## Restricciones y riesgos

El log de CI no incluye tiempos de cada reserva: el diagnóstico del cruce temporal se fundamenta en código y reproducción controlada. No afirmar acceso a un nuevo run remoto ni éxito de CI sin evidencia. Preservar SQLite real y los clientes externos simulados.

## Preguntas y decisiones

Mantener ventanas fijas existentes. Sustituir solo la referencia time del módulo de cuotas por un objeto simulado; no parchear time.time global. Separar la semántica de frontera de la prueba HTTP.

## Revisiones

| Revisión | Cambio | Referencia de la decisión humana |
|---|---|---|
| 1 | Arreglar prueba intermitente reportada en CI | Mensaje del usuario y log adjunto |
