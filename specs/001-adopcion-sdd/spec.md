# 001-adopcion-sdd — Control de cambios con SDD

## Problema y resultado esperado

El usuario necesita controlar el alcance de la IA en cambios futuros: revisar lo que se implementará y contrastar el resultado con requisitos/pruebas. Esta adopción documenta la solicitud ya autorizada en la conversación; la referencia es el mensaje original, no una aceptación del resultado final ni una aprobación histórica de funciones.

## Alcance

Constitución, plantillas, reglas AGENTS, baseline descriptivo, guía de uso, scaffold/checker local, plantilla PR y comprobación CI. La preparación del flujo es el cambio actual.

## Fuera de alcance

Cambiar comportamiento del chatbot, proveedores/modelos/precios, migrar bases, instalar Spec Kit, comandos slash, controlar permisos remotos o desplegar. No se pretende probar consentimiento humano mediante JSON ni demostrar corrección semántica mediante regex.

## Requisitos

- **FR-001**: Conservar principios y artefactos spec/plan/tasks/validation versionados por cambio.
- **FR-002**: Crear un borrador local identificado sin implementar, aprobar, sobrescribir ni salir de specs.
- **FR-003**: Comprobar documentos y trazabilidad de requisitos/criterios/tareas; distinguir autorización vigente, tarea terminada, verificación y aceptación humana.
- **FR-004**: Integrar la comprobación estructural en CI y la revisión de alcance/evidencia en PR.
- **NFR-001**: Usar Python 3.13/stdlib, comandos de Windows/Linux y ninguna dependencia nueva ni llamada externa durante la comprobación.

## Criterios de aceptación

- **AC-001** [FR-001, FR-002]: Al crear 002-feature se generan los cinco artefactos con estado draft y authorization null; las plantillas quedan reutilizables.
- **AC-002** [FR-002]: Nombres de escape, número duplicado y sobrescritura fallan sin crear una feature adicional; plantilla ausente no deja una carpeta parcial.
- **AC-003** [FR-003]: Referencias desconocidas, cobertura ausente, archivos faltantes, IDs duplicados, autorización de revisión anterior y tareas incompletas se rechazan según el estado.
- **AC-004** [FR-003]: verified exige evidencia técnica sin marcadores pendientes, pero no aceptación humana; accepted requiere referencia de aceptación de la misma revisión.
- **AC-005** [FR-004, NFR-001]: CI ejecuta check en Ubuntu/Windows; las pruebas locales y Ruff pasan sin modificar dependencias o runtime. La ejecución remota de CI se informa por separado.

## Restricciones y riesgos

Mantener invariantes de seguridad y datos existentes. Las referencias de autorización deben provenir del usuario; el checker no autentica identidad ni interpreta el alcance. Protección de ramas/revisión GitHub no se configura aquí. Pruebas locales en Windows no prueban Linux.

## Preguntas y decisiones

Adoptar un flujo ligero inspirado en la documentación de GitHub Spec Kit, sin instalarlo. La solicitud autoriza preparación del proceso. La aceptación de los artefactos entregados queda reservada al usuario. Usar el mensaje humano real como referencia de autorización.

## Revisiones

| Revisión | Cambio | Referencia de la decisión humana |
|---|---|---|
| 1 | Adopción inicial del flujo | Mensaje original registrado en feature.json |
