> **Seguridad actualizada:** leer [docs/SEGURIDAD.md](docs/SEGURIDAD.md). Las rutas de chat y documentos ahora requieren Bearer; los túneles publican únicamente webhooks/health. Instalar con `uv pip sync requirements-dev.lock --require-hashes` y ejecutar la suite con Python 3.13. Ejemplos antiguos de curl sin autenticación deben añadir `Authorization: Bearer <clave>` y usar la API interna privada.

# chatbot-pymes

Chatbot/RAG como asistente virtual para pequeños y medianos negocios (pymes).

Responde las preguntas de los clientes usando **únicamente** la información del
negocio que hayas indexado (archivos `.txt` o `.pdf`). Si la respuesta no está
en esos documentos, el chatbot lo dice y sugiere contactar al negocio: nunca
inventa datos.

## Cómo funciona

- Backend [FastAPI](https://fastapi.tiangolo.com/) (`app/main.py`).
- El archivo(s) con los datos del negocio se sube por API, se trocea y se indexa
  en un índice SQLite local con similitud coseno (`app/rag/indexing.py` / `app/persistence/vector_store.py`).
- Cada pregunta del cliente recupera los fragmentos más relevantes
  (5 por defecto) y se los pasa al modelo de lenguaje como contexto.
- **Modelo de embeddings**: `intfloat/multilingual-e5-small` en ONNX
  (`app/rag/embeddings.py`), con soporte para español. Se descarga una sola vez a
  `~/.cache/huggingface` (~226 MB) y corre 100% local, sin API keys.
- **Modelo de lenguaje**: se intentan en orden los proveedores configurados
  (Groq → OpenRouter → Gemini) con *fallback* automático si uno falla o está
  limitado por tasa.

## Requisitos

- Python 3.13
- API key de al menos un proveedor (ver abajo)

## Instalación

```bash
uv venv --python 3.13
uv pip sync requirements-dev.lock --require-hashes

cp .env.example .env   # completa al menos una API key
```

Primera ejecución: al indexar o buscar, se descargará el modelo de embeddings
(~226 MB). Se necesita internet solo esa vez.

## Configuración (`.env`)

| Variable                | Proveedor/Canal                                | Cómo obtener la key                    |
| ----------------------- | ---------------------------------------------- | -------------------------------------- |
| `GROQ_API_KEY`          | Groq (rápido y gratuito, recomendado)          | https://console.groq.com/keys          |
| `OPENROUTER_API_KEY`    | OpenRouter (muchos modelos, incl. gratis)      | https://openrouter.ai/settings/keys    |
| `GEMINI_API_KEY`        | Google Gemini                                  | https://aistudio.google.com/apikey     |
| `TELEGRAM_BOT_TOKEN`    | Bot para hablar desde Telegram                 | @BotFather (`/newbot`)                 |
| `TELEGRAM_WEBHOOK_SECRET`| Secreto para validar el webhook de Telegram   | Generar uno (o `scripts/run_bot.sh` lo crea)   |

Los proveedores sin key se ignoran automáticamente. Con una sola key el chat
funciona; con varias, si el primero falla se pasa al siguiente.

## Cargar los datos del negocio (la única fuente de información)

```bash
# Inicia el servidor (el puerto 8000 lo usa el document-service)
uv run --no-project uvicorn app.main:app --host 127.0.0.1 --reload --port 8005

# Sube tu archivo (.txt o .pdf). Reemplaza el ejemplo por tus datos reales.
curl -X POST http://127.0.0.1:8005/documents/upload \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -F "file=@data/business_data.txt"
```

Hay un documento de ejemplo en `data/business_data.txt` (una panadería ficticia)
para probar. El contenido que indexes será lo único que el chatbot conozca.

> Si reemplazas los datos del negocio, reindexar documentos originales por la API administrativa. No borrar bases automáticamente; consultar docs/SEGURIDAD.md para migración y backup.

## API

| Método | Ruta                          | Descripción                                   |
| ------ | ----------------------------- | --------------------------------------------- |
| POST   | `/chat`                       | Envía una pregunta y recibe la respuesta       |
| POST   | `/documents/upload`           | Sube e indexa un `.txt` o `.pdf`               |
| GET    | `/documents`                  | Lista los documentos indexados                 |
| DELETE | `/documents/{source_name}`    | Elimina un documento del índice                |
| POST   | `/documents/quote`            | Genera una cotización PDF vía document-service |
| GET    | `/health`                     | Estado y número de proveedores configurados    |
| POST   | `/webhooks/telegram`          | Webhook que recibe los mensajes de Telegram    |
| GET    | `/webhooks/whatsapp`          | Verificación del webhook de WhatsApp (Meta)    |
| POST   | `/webhooks/whatsapp`          | Webhook que recibe los mensajes de WhatsApp    |

Ejemplo de chat:

```bash
curl -X POST http://127.0.0.1:8005/chat \
  -H "Authorization: Bearer $CLIENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "¿Cuánto cuesta el pan de masa madre?"}'
```

Usa la clave de un cliente configurado en `API_CLIENT_KEYS` para chat y `ADMIN_API_KEY` para documentos. Los ejemplos Bash suponen esas claves exportadas como `CLIENT_API_KEY` y `ADMIN_API_KEY`; la aplicación lee `.env`, pero el shell no las exporta automáticamente.

La respuesta incluye el `provider` y el `model` usados, y si se usó contexto RAG.

El campo opcional `conversation_id` activa memoria de la conversación (últimos
mensajes del hilo): el bot evita repetir datos que ya dio antes (p. ej. el
contacto) y solo los retoma si el usuario los pide o se despide. Sin ese campo,
cada pregunta es independiente.

### Generar una cotización (PDF)

`/documents/quote` delega en el microservicio `document-service` (requiere
`DOCUMENT_SERVICE_URL` e `INTERNAL_API_KEY` configurados):

```bash
curl -X POST http://127.0.0.1:8005/documents/quote \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "req-001",
    "customer": {"name": "Juan Pérez", "phone": "809-555-0101"},
    "items": [{"description": "Pan francés", "quantity": 1, "unit_price": 0.0}],
    "currency": "DOP"
  }'
```

Devuelve `{document_id, quote_number, pdf_url, size_bytes, status}`. Si el
document-service no está disponible o rechaza la petición, responde `502`.

### Envío de correos (email-service)

`app/integrations/email.py` es un cliente del microservicio `email-service`, que entrega
los correos con Resend. **Todavía no lo invoca ningún flujo del bot**: está
escrito y verificado contra el servicio real, pero desconectado a propósito.

```python
from app.integrations import email as email_service

resultado = await email_service.send_email(
    tenant_id="cliente_demo",
    to="cliente@example.com",           # texto suelto o lista
    subject="Tu cotización",
    text="Adjuntamos tu cotización.",
    attachments=[email_service.encode_attachment("cotizacion-001.pdf", pdf_bytes, "application/pdf")],
    idempotency_key=email_service.build_idempotency_key("quote-email", "quote-001"),
)
```

Devuelve `EmailResult(email_id, tenant_id)` o `None` (con `logger.warning`) si
falta configuración, hay error de red o el servicio rechaza el envío.

Tres cosas que conviene no pasar por alto:

- **La `Idempotency-Key` no es opcional en la práctica.** El email-service nunca la genera; sin ella, un reintento vuelve a enviar el correo. Usa `build_idempotency_key()` con un identificador estable del envío.
- **Misma clave + mismo cuerpo → el mismo `email_id`** (no duplica). Misma clave + cuerpo distinto → `502 RESEND_SEND_FAILED`, porque Resend rechaza con 409 al reutilizarla en 24 h.
- **El remitente no lo elige el bot:** lo pone el email-service desde su `EMAIL_FROM`. Esta clave es distinta de `INTERNAL_API_KEY` (la de document-service), y la API key de Resend nunca sale del email-service.

Configuración: `EMAIL_SERVICE_URL` (por defecto `http://127.0.0.1:8003`) y
`EMAIL_SERVICE_API_KEY`. Si el chatbot también corre en Docker, la URL pasa a
`http://email-service:8003`.

> Con `EMAIL_FROM=onboarding@resend.dev` (modo pruebas de Resend) solo se puede
> enviar a la dirección de la cuenta de Resend, así que una prueba real va
> únicamente a tu propio correo hasta verificar el dominio.

### Cotizaciones por chat (PDF adjunto)

Cuando un cliente pide una cotización por WhatsApp o Telegram, el bot no
responde con texto: extrae la estructura (ítems, cantidades y precios según la
información del negocio) con el LLM, genera el PDF en el document-service y lo
envía como **archivo adjunto** (`sendDocument` en Telegram; media + mensaje tipo
`document` en WhatsApp). Nunca se envía el PDF en base64 ni como instrucciones.

El teléfono del cliente se rellena con el número del canal si el LLM no lo
detecta. Si la extracción falla (p. ej. faltan precios en la información del
negocio), el mensaje se responde por el flujo normal de `/chat`.

## Bot de Telegram

Para hablarle al chatbot desde tu celular, sin dominio ni configuración manual:

```bash
# Requisitos
# 1. Crea el bot con @BotFather y pon el token en .env: TELEGRAM_BOT_TOKEN
# 2. Instala cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
#    (o descárgalo como ./cloudflared)

# Levanta uvicorn + un túnel HTTPS de Cloudflare y registra el webhook
./scripts/run_bot.sh start

# Detener
./scripts/run_bot.sh stop

# Re-registrar el webhook si cambió la URL del túnel (por un reinicio)
./scripts/run_bot.sh webhook
```

`scripts/run_bot.sh` genera `TELEGRAM_WEBHOOK_SECRET` automáticamente si no existe y lo
guarda en `.env`. Abre tu bot en Telegram, pulsa *Start* y escribe.

> La URL gratuita de `trycloudflare.com` cambia en cada reinicio del túnel;
> vuelve a ejecutar `./scripts/run_bot.sh webhook` (o `start`) para re-registrarla.
> Las conversaciones se guardan en SQLite con TTL acotado; un reinicio conserva
> las sesiones que todavía no han expirado.

## WhatsApp Business (Cloud API de Meta)

Conecta el chatbot a tu número de WhatsApp del negocio usando la Cloud API de
Meta. Como no tienes dominio aún, `scripts/run_whatsapp.sh` abre un túnel HTTPS de
Cloudflare (misma idea que Telegram) y te imprime la URL para el panel.

Requisitos en [developers.facebook.com](https://developers.facebook.com/):

- App creada y enlazada a un número de WhatsApp Business (WhatsApp > API Setup).
- `WHATSAPP_PHONE_NUMBER_ID` — id del número del negocio.
- `WHATSAPP_ACCESS_TOKEN` — token temporal (24 h) del panel, o mejor uno
  **permanente** de system user (Empresa >  Configuración de la empresa >
  Usuarios del sistema) con permiso `whatsapp_business_messaging`.
- `WHATSAPP_APP_SECRET` — App Settings > Basic (para validar la firma).
- `WHATSAPP_VERIFY_TOKEN` — una cadena cualquiera que elijas.
- `cloudflared` instalado (ver sección de Telegram).

Configuración:

```bash
# 1. Completa las 4 variables WHATSAPP_* en .env
# 2. Levanta servidor + túnel (puerto 8002, no choca con el del bot de Telegram en 8001)
./scripts/run_whatsapp.sh start
```

El script imprime la URL pública. Después, en el panel de Meta:

1. Tu app → WhatsApp → Configuration → Webhook.
2. Callback URL: `https://<túnel>.trycloudflare.com/webhooks/whatsapp`
3. Verify token: el valor de `WHATSAPP_VERIFY_TOKEN`.
4. Pulsa *Verify and save* (el servidor responde al handshake de verificación).
5. En *Webhook fields*, suscríbete a `messages` (sin esto no llega nada).

Listo: escribe al número del negocio desde tu teléfono y el chatbot responde.

> **Ventana de 24 h**: la Cloud API solo permite mensajes de texto libre como
> respuesta dentro de las 24 h posteriores a un mensaje del cliente. Fuera de
> esa ventana hace falta una plantilla aprobada por Meta.
>
> **Token temporal**: caduca en ~24 h; si respondes 401 al enviar, regenera el
> token en el panel o crea uno permanente de system user.
>
> **URL del túnel**: la URL gratuita de `trycloudflare.com` cambia en cada
> reinicio; si reinicias, vuelve a ejecutar `./scripts/run_whatsapp.sh start` y repite
> el paso 2 con la nueva URL.

## Facebook Messenger (Messenger Platform de Meta)

Conecta el chatbot a la **Página de Facebook** del negocio para que te escriban
por Messenger. Igual que Telegram y WhatsApp, `scripts/run_messenger.sh` abre un túnel
HTTPS de Cloudflare y te imprime la URL para pegarla en el panel de Meta.

El endpoint público es `GET/POST /webhooks/messenger` (puerto **8004**).

Requisitos en [developers.facebook.com](https://developers.facebook.com/):

- App con Messenger habilitado y **una Página de Facebook** asociada
  (no sirve un perfil personal).
- `MESSENGER_PAGE_ID` — id numérico de la Página.
- `MESSENGER_ACCESS_TOKEN` — token de la **Página** con el permiso
  `pages_messaging` (generado en el Centro de estados / tu app).
- `MESSENGER_APP_SECRET` — App Settings > Basic (para validar la firma).
  Si la app es la misma que WhatsApp, puede ser el mismo valor.
- `MESSENGER_VERIFY_TOKEN` — una cadena cualquiera que elijas.
- `cloudflared` instalado (ver sección de Telegram).

Configuración:

```bash
# 1. Completa las 4 variables MESSENGER_* en .env
# 2. Levanta servidor + túnel (puerto 8004; no choca con 8001 Telegram ni 8002 WhatsApp)
./scripts/run_messenger.sh start
```

El script imprime la URL pública. Después, en el panel de Meta:

1. Tu app → Messenger → Settings / Webhooks (o App Settings → Webhooks).
2. Callback URL: `https://<túnel>.trycloudflare.com/webhooks/messenger`
3. Verify token: el valor de `MESSENGER_VERIFY_TOKEN`.
4. Pulsa *Verify and save* (el servidor devuelve el `hub.challenge` en texto plano).
5. En *Webhook fields*, suscríbete a `messages` (sin esto no llega nada).
6. Abre tu Página en Facebook/Messenger y escribe al bot.

Listo: te responde dentro de la conversación de Messenger.

> **Ventana de 24 h**: igual que WhatsApp, solo se puede mandar texto libre
> dentro de las 24 h posteriores al último mensaje del cliente; fuera de esa
> ventana Meta solo acepta plantillas aprobadas. Los errores de envío se
> registran en el log y no interrumpen el webhook.
>
> **Ecos propios**: si alguien escribe desde la Página (por ejemplo desde
> Facebook, como administrador) Meta devuelve el mensaje con `is_echo` o en
> `message_echoes`. Se descartan para que el bot **no responda a sí mismo**.
>
> **Adjuntos**: los `mid` de Messenger solo se obtienen en el payload entrante.
> El texto largo se parte en trozos de 2000 caracteres, el límite de Messenger.
> Los archivos llegan en `message.attachments` y **no se interpretan** (solo
> texto), igual que el resto de canales; un PDF pedido por cotización se
> **envía** como archivo vía `message_attachments` (límite 25 MB).
>
> **URL del túnel**: la URL gratuita de `trycloudflare.com` cambia en cada
> reinicio; si reinicias, vuelve a ejecutar `./scripts/run_messenger.sh start` y repite
> el paso 2 con la nueva URL.

## Estructura

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

La dirección de dependencias es: rutas/CLI/workers → servicios → integraciones/persistencia. `core/config.py` centraliza las rutas desde la raíz del proyecto; `main.py` solo compone la aplicación. Los paquetes usan imports absolutos bajo `app`.

## Limitaciones conocidas

- Sin API key, `/chat` devuelve 503 hasta que configures al menos una.
- La velocidad de indexado/búsqueda depende de tu CPU (los embeddings corren en
  local). En hardware modesto cada búsqueda tarda unos segundos.
- `rag.sqlite3` es el índice vectorial; `inbox.sqlite3` conserva trabajos y sesiones temporales. Ambos se excluyen de Git. No eliminar `chroma_db/` antiguo sin backup.
## Desarrollo y rutas locales

Ejecutar desde la raíz del repositorio:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m app.cli.cost_report --period 2026-10
```

En Linux usar `.venv/bin/python`. Los scripts Bash se ejecutan como `./scripts/run_bot.sh start` (o los otros canales). Ver [arquitectura y migración de rutas](docs/ESTRUCTURA.md).
