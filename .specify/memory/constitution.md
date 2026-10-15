# Constitución del proyecto

Versión: 1.0.0 — 2026-10-14. Propuesta inicial derivada de las reglas y del código existentes.

## 1. El alcance procede del usuario

Cada cambio de comportamiento parte de una especificación en `specs/NNN-nombre/`. Requisitos, exclusiones y criterios de aceptación son la referencia del trabajo. La IA prepara artefactos revisables antes de solicitar autorización para implementar; no inventa aprobaciones ni atribuye decisiones al usuario. Una autorización humana puede abarcar especificación, plan y tareas juntos. No volver a pedir permiso para pasos ya autorizados dentro de ese alcance.

Una ampliación material de alcance, contrato, coste, datos o permisos requiere actualizar la especificación y recibir autorización para la revisión afectada. Elegir detalles rutinarios dentro del plan autorizado no requiere otra aprobación. El código no convierte retroactivamente una idea en un requisito aprobado.

## 2. Seguridad y datos

Conservar separación entre API privada con Bearer y aplicación pública de webhooks, secretos/HMAC obligatorios, aislamiento de conversaciones, catálogo de precios autoritativo, límites/cuotas, parser PDF aislado y logs redactados. No incluir credenciales, datos personales reales, mensajes de clientes ni PDFs en especificaciones o evidencias. Usar datos ficticios.

Los jobs se confirman después de persistir; las sesiones y cuotas usan SQLite compartido con leases/TTL. Nunca afirmar entrega exactamente una vez ni eliminar/mover bases por iniciativa propia. Cambios de esquema incluyen compatibilidad, backup y rollback. No habilitar respuestas Instagram o multitenencia sin un cambio especificado.

## 3. Arquitectura y compatibilidad

Python 3.13, FastAPI/Pydantic, imports absolutos `app.*`. Rutas HTTP en `app/api`, lógica en `app/services`, HTTP externo en `app/integrations`, SQL en `app/persistence`, procesos en `app/workers`. `app/main.py` compone aplicaciones. Las rutas parten de `app/core/config.py`. Windows y Linux son plataformas de validación.

No introducir abstracciones o dependencias sin explicar su necesidad en el plan. Mantener Groq → OpenRouter → Gemini, modelos/revisión E5 y medición real de usage salvo autorización específica. Dependencias reproducibles con locks y hashes; actualizar ambos locks y revisar auditoría cuando cambien.

## 4. Evidencia antes de declarar terminado

Cada criterio de aceptación tiene verificación reproducible: prueba automatizada o revisión manual justificada. Para fallos reproducibles, agregar una regresión que compruebe el síntoma; para nuevas funciones, cubrir resultados, errores y límites pertinentes. No probar detalles internos solo para aumentar el conteo.

Usar unittest y clientes externos simulados. Ejecutar suite pertinente, Ruff y controles requeridos por el cambio; la suite completa es obligatoria al cambiar comportamiento del backend o utilidades ejecutables. No enviar mensajes reales, descargar modelos o usar servicios externos en pruebas unitarias. Registrar lo ejecutado, resultado, entorno y lo que no se verificó. Pruebas de Windows no prueban Linux.

No cambiar criterios para hacer pasar una implementación, debilitar auth o ignorar tests fallidos. Tareas terminadas y validación técnica no equivalen a aceptación humana ni a permiso de despliegue.

## 5. Historial y mantenimiento

Mantener spec, plan, tareas y validación junto al código. Documentar decisiones y desviaciones; conservar evidencia de autorizaciones auténticas. Una modificación posterior usa nueva revisión o nueva spec. Las actualizaciones de esta constitución describen motivo/impacto y se autorizan como cambios de proceso.
