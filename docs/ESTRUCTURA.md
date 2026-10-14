# Estructura y ejecución

```text
app/
  main.py                 # Fábrica de aplicaciones y ciclo de vida
  api/                    # Rutas de chat, documentos, webhooks y health
  core/                   # Configuración, seguridad y recursos compartidos
  schemas/                # Contratos Pydantic de entrada/salida
  services/               # Chat, proveedores LLM, cotizaciones, sesiones y costes
  integrations/
    channels/             # Telegram, WhatsApp, Messenger e Instagram
    documents.py          # Cliente del servicio de PDFs
    email.py              # Cliente del servicio de correos
  rag/                    # Ingesta, embeddings y proceso aislado del parser PDF
  persistence/            # SQLite: trabajos, sesiones, cuotas, métricas e índice
  workers/                # Consumo de inbox y cierre por inactividad
  cli/                    # Informe de costes ejecutable como módulo
scripts/                  # Arranque por canal y lectura segura de dotenv
docs/                     # Seguridad, planes y documentación técnica
data/                     # Documentos y catálogo aprobados del negocio
runtime/                  # Bases, logs y PID locales; excluidos de Git
tests/                    # Pruebas por área, siguiendo los paquetes de app
.github/workflows/        # CI, auditoría de dependencias y secretos
```

## Responsabilidades

`app/main.py` crea dos aplicaciones FastAPI, registra routers y arranca/cancela los workers. `app/api` autentica/valida HTTP y delega. `app/services` contiene chat, fallback LLM, cotizaciones, catálogo, sesiones y costes. `app/integrations` encapsula HTTP externo. `app/persistence` contiene SQL y transacciones; `app/workers` procesa trabajos y vencimientos. `app/schemas` comparte contratos sin depender de rutas. El estado efímero y límites comunes están en `app/core/state.py`.

Los módulos usan imports absolutos `app.*`. Ejecutar comandos desde la raíz del repositorio permite resolver el paquete sin modificar `sys.path`. Las pruebas están agrupadas por área. `scripts/` contiene utilidades operativas, `docs/` la documentación técnica y `data/` los documentos y catálogo del negocio.

## Comandos nuevos

API interna: `uv run --no-project uvicorn app.main:app --host 127.0.0.1 --port 8005`.

Aplicación pública: `app.main:public_app`. Los scripts ya usan este entrypoint:

```bash
./scripts/run_bot.sh start
./scripts/run_whatsapp.sh start
./scripts/run_messenger.sh start
```

Informe de costes: `uv run --no-project python -m app.cli.cost_report --period 2026-10`. La opción `--db` también resuelve rutas relativas desde la raíz del proyecto.

Suite: `uv run --no-project python -m unittest discover -s tests -v`. Lint: `uv run --no-project python -m ruff check .`.

## Datos existentes y runtime

`app/core/config.py` carga el `.env` de la raíz sin reemplazar variables del proceso. Las rutas relativas de INBOX_DB_PATH, METRICS_DB_PATH, RAG_DB_PATH, QUOTE_CATALOG_PATH y RUNTIME_DIR se anclan en esa raíz, independientemente del directorio de trabajo. Nuevas bases y logs usan `runtime/`; el catálogo sigue en `data/quote_catalog.json`.

La reorganización no traslada bases existentes. Una variable explícita de ruta tiene prioridad. Si no hay una, cada base antigua (`inbox.sqlite3`, `metrics.sqlite3`, `rag.sqlite3`) situada en la raíz se conserva; únicamente una base inexistente adopta `runtime/`. No reemplazar el `.env` existente con el ejemplo sin revisar las rutas: el ejemplo configura `runtime/*.sqlite3` explícitamente.

Para trasladar datos después: detener todas las instancias y escritores, respaldar las bases con sus archivos WAL/SHM si existen, trasladar con un procedimiento de backup SQLite y actualizar las rutas de todas las instancias. Verificar jobs/sesiones, eventos y fuentes antes de cambiar tráfico. Conservar el respaldo para rollback. Los logs/PID nuevos de scripts se guardan en `runtime/logs`; detener procesos arrancados con scripts anteriores antes de usar los nuevos.

`runtime/` queda excluido de Git salvo `.gitkeep`. Documentos en `data/` no se publican automáticamente; revisar su contenido antes de versionarlos. Se mantiene el alcance de un solo negocio con TENANT_ID configurable.
