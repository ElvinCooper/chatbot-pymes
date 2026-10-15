# Punto de partida para SDD

Fecha: 2026-10-14. Referencia de código: commit `48f67aa`. Documento descriptivo; no constituye aprobación retroactiva de funciones ni autorización para ampliarlas.

## Comportamiento existente

- API privada `app.main:app`: chat y administración documental con Bearer. App pública `app.main:public_app`: health/webhooks.
- Telegram, WhatsApp y Messenger procesan texto/cotizaciones; Instagram recibe sin responder.
- RAG local E5/ONNX y SQLite; precios autoritativos desde catálogo aprobado.
- Jobs/sesiones/cuotas con persistencia SQLite; reintentos y checkpoints, sin garantía de entrega exactamente una vez.
- Métricas/costes e informe CLI. Un solo negocio con TENANT_ID configurable; no hay multitenencia implementada.

Fuentes del estado: `app/`, [estructura](../docs/ESTRUCTURA.md), [seguridad](../docs/SEGURIDAD.md) y `tests/`. Los tests y el código prevalecen para verificar comportamiento actual; cualquier cambio deseado se especifica aparte.

## Evidencia disponible

En la reorganización se ejecutaron 64 pruebas unittest y Ruff en Windows con Python 3.13. Se registró Gitleaks sin hallazgos. No se ejecutó la suite en Linux ni se probaron proveedores/canales reales. El workflow CI tiene matriz Ubuntu/Windows, pero su configuración no demuestra que una ejecución remota haya pasado.

## Invariantes

Autenticación y firmas obligatorias, aislamiento de sesiones, cuotas/límites, catálogo autoritativo, protección de datos y bases existentes. Ver [.specify/memory/constitution.md](../.specify/memory/constitution.md).
