# Seguridad y operación

La API interna (`app.main:app`) exige `Authorization: Bearer <clave>` para `/chat` y administración. `ADMIN_API_KEY` habilita administración; `API_CLIENT_KEYS` asigna una identidad distinta a cada cliente. Claves vacías o menores de 32 caracteres no habilitan acceso. Las sesiones API se derivan de la identidad autenticada y un ID local al cliente, sin prefijos de WhatsApp/Telegram/Messenger. No hay multitenencia: `TENANT_ID` es único y no se acepta otro tenant en cotizaciones.

Los scripts exponen `app.main:public_app`, con únicamente `/health` y webhooks. Para usar API administrativa, levantar `app.main:app` en otro puerto privado (por ejemplo 8005), detrás de TLS y controles de red. Nunca publicar esta API directamente en un túnel. No usar 8000: pertenece al servicio de documentos. La documentación automática se deshabilita en ambas aplicaciones.

## Configuración

Crear `.env` desde `.env.example`. Generar claves distintas con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. No guardar estas claves en Git ni usar las de servicios internos para clientes. WhatsApp/Messenger requieren App Secret; Telegram requiere webhook secret; Instagram requiere `INSTAGRAM_APP_SECRET`. Un verify token de Meta solo valida el handshake y no reemplaza la firma de cada POST.

El ejemplo de catálogo corresponde a los precios del archivo existente `data/business_data.txt`. Mantener `data/quote_catalog.json` desde fuentes aprobadas del negocio. El LLM identifica productos y cantidades; el código reemplaza descripción y precio por los del catálogo. Productos desconocidos, moneda distinta o personalizados con `requires_review: true` se derivan al negocio y no generan una cotización definitiva. Un administrador también usa precios autoritativos. No hay endpoint de aprobación automática ni descuento decidido por el modelo.

## Instalación y pruebas reproducibles

Python 3.13 (misma versión en CI):

```powershell
uv venv --python 3.13
uv pip sync requirements.lock --require-hashes
# Para desarrollo, lint y auditoría de dependencias:
uv pip sync requirements-dev.lock --require-hashes
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pip_audit --local
```

En Linux usar `.venv/bin/python`. Los tests mockean LLM/Meta/document-service y no descargan embeddings ni envían mensajes reales. La revisión del modelo ONNX/tokenizer se fija en `app/rag/embeddings.py` al commit verificado del repositorio de Hugging Face. Para actualizar dependencias, resolver nuevamente ambos locks con uv, revisar auditoría, cambios de API y ejecutar la suite antes de distribuirlos.

Las rutas y el almacenamiento tras la reorganización se describen en [ESTRUCTURA.md](ESTRUCTURA.md).

## Límites y datos

- HTTP: 256 KiB por cuerpo general y 6 MiB para multipart; archivo máximo 5 MiB; mensajes de 4000 caracteres; cotizaciones de 50 ítems.
- Recepción HTTP: 10 s sin recibir un fragmento y 60 s en total; scripts limitan conexiones concurrentes a 32 y desactivan access logs para evitar registrar tokens de handshake.
- Ingesta: 100 páginas PDF, 300 000 caracteres/600 chunks; parser PDF en un proceso separado, timeout 15 s y memoria limitada (256 MiB en Windows, 512 MiB en Unix).
- API: 20 solicitudes de chat por minuto por identidad; documentos/cotizaciones administrativas 10/minuto. Webhooks 120/minuto por canal, mensajes procesados 20/minuto por contacto. Cuotas se comparten en SQLite, por ventanas fijas.
- LLM: máximo 4 llamadas concurrentes por proceso, salida 1000 tokens y cero reintentos internos del SDK. Se conserva Groq → OpenRouter → Gemini. Reservas globales diarias por llamadas y por tokens (bytes UTF-8 de entrada + salida máxima, una cota conservadora). Las métricas siguen usando únicamente usage real del proveedor.
- Sesiones: 500, TTL de 10 minutos por defecto, SQLite hasta 100 MiB de contenido lógico; caché PDF en memoria hasta 20 MiB. Respuestas pendientes hasta 50 MiB; cola hasta 500 trabajos.
- `inbox.sqlite3` contiene temporalmente mensajes/cotizaciones y PDFs codificados; proteger permisos y backups como datos de clientes. Sesiones expiradas se eliminan al consultar/guardar y trabajos finalizados borran payload/plan. IDs de dedup se conservan siete días. Borrado lógico de SQLite no garantiza eliminación forense de páginas o backups; cifrado/retención del almacenamiento debe configurarse en despliegue.
- El plazo de cierre se conserva separado del contenido expirado. El monitor reclama ese plazo con una lease, incluso tras reinicio; un mensaje entrante auténtico pausa el cierre. Trabajos sin procesar caducan a las 24 h; máximo 200 000 IDs de historial. Cuotas expiradas se limpian entre scopes para no crecer indefinidamente.

## Procesamiento durable y reintentos

El webhook confirma 200 solo después de insertar en inbox SQLite. El worker consume trabajos con lease y estados pending/processing/completed/failed, máximo tres intentos; conserva orden por conversación incluso con consumidores distintos. Un fallo de métricas es best-effort; un fallo de inbox devuelve 503 y permite retry del proveedor.

Se guarda un checkpoint del borrador antes de generar documento y de la respuesta antes de enviarla. El request_id documental deriva del tenant, canal e ID del evento, incluso si se recrea la inbox; los reintentos reutilizan borrador y request_id. El transporte devuelve bool y solo se marca aceptación con confirmación HTTP. Una aceptación no equivale a entrega al destinatario.

**Límite externo:** Meta/Telegram no ofrecen aquí una clave idempotente del envío. Una caída después de que el proveedor acepta y antes del checkpoint local puede duplicar un mensaje. El comportamiento es al menos una vez, no exactamente una vez. Messenger además puede haber enviado caption o parte del texto antes de fallar. No se puede cerrar esta ventana únicamente desde SQLite. Los trabajos agotados quedan failed sin contenido sensible y requieren supervisión operativa; no se reintentan indefinidamente.

Ejecutar una instancia por canal, con BOT_CHANNEL correspondiente y el mismo INBOX_DB_PATH para consumidores del mismo despliegue. La API interna y los canales usan leases SQLite por conversación, con timeout de 180 s inferior a la lease de 240 s, para serializar escritores entre procesos. El índice SQLite reemplaza fuentes en una transacción y admite escritores serializados por SQLite. La búsqueda coseno exacta se limita a 20 000 fragmentos para acotar memoria; para corpus mayores evaluar un motor distinto con revisión de seguridad.

## Verificaciones de despliegue pendientes fuera de este repositorio

El chatbot descarga PDFs solo desde el origen configurado del servicio, envía su clave interna, limita bytes, verifica MIME/firma y no sigue redirects. **El document-service debe exigir autenticación o enlaces firmados con caducidad en sus rutas de archivos**; el cliente no puede impedir que un servidor externo publique PDFs sin protección.

Revisar acceso a logs y backups históricos. No se revocaron credenciales ni se reescribió Git porque la auditoría no confirmó una clave real expuesta. Si se encuentra una, revocarla primero y coordinar limpieza de historial. Revisar también precios externos/tarifas antes de uso comercial; esta corrección no cambia tarifas ni modelos.

La separación de roles reduce prompt injection, pero no asegura que un LLM jamás siga instrucciones maliciosas. El catálogo autoritativo, aislamiento de sesión y permisos contienen los efectos determinantes; verificar respuestas adversarias con los proveedores reales antes de producción.

## Migración del índice y avisos de ChromaDB

La auditoría OSV detectó cuatro avisos únicos en ChromaDB 1.5.9 (el servicio los devolvió duplicados): CVE-2026-45829, CVE-2026-45830, CVE-2026-45831 y CVE-2026-45833. Afectan a rutas de servidor/autorización y carga de modelos remotos; las fichas no indican parche. Se retiró completamente ChromaDB del código y locks, sin ocultar los avisos en pip-audit. E5/ONNX, sus prefijos y la revisión fijada se mantienen. El almacenamiento usa SQLite con consultas parametrizadas, vectores float32 finitos, coseno exacto e ingesta por fuente transaccional.

No se modifica ni elimina `chroma_db/` antiguo. Para migrar:

1. Respaldar esa carpeta y los documentos originales; detener la ingesta en la instancia anterior.
2. Instalar el nuevo lock con uv y configurar RAG_DB_PATH en una ruta nueva.
3. Levantar `app.main:app` privado y reingestar originales por `/documents/upload` con Bearer administrativo (el archivo de ejemplo es `data/business_data.txt`). El modelo fijado se descarga en la primera ingesta si no está en caché.
4. Verificar listado de fuentes y respuestas conocidas antes de cambiar tráfico. Mantener el respaldo para rollback. No hay conversión automática de binarios Chroma ni borrado de originales.

En esta copia del proyecto no había `chroma_db/` local. Las pruebas del nuevo índice usan SQLite real y embeddings ficticios; no se descargó ni evaluó el modelo ONNX durante esta corrección.

Fuentes: [aviso de inyección previa a autenticación](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c), [inyección al actualizar colección](https://github.com/advisories/GHSA-36p7-vc44-83pf), [acceso entre tenants](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr), [autorización RBAC](https://github.com/advisories/GHSA-xph7-9rjv-w5fr).
