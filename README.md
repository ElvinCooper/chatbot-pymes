# chatbot-pymes

Chatbot/RAG como asistente virtual para pequeños y medianos negocios (pymes).

Responde las preguntas de los clientes usando **únicamente** la información del
negocio que hayas indexado (archivos `.txt` o `.pdf`). Si la respuesta no está
en esos documentos, el chatbot lo dice y sugiere contactar al negocio: nunca
inventa datos.

## Cómo funciona

- Backend [FastAPI](https://fastapi.tiangolo.com/) (`main.py`).
- El archivo(s) con los datos del negocio se sube por API, se trocea y se indexa
  en una base vectorial local [ChromaDB](https://www.trychroma.com/) (`rag.py`).
- Cada pregunta del cliente recupera los fragmentos más relevantes
  (5 por defecto) y se los pasa al modelo de lenguaje como contexto.
- **Modelo de embeddings**: `intfloat/multilingual-e5-small` en ONNX
  (`embeddings.py`), con soporte para español. Se descarga una sola vez a
  `~/.cache/huggingface` (~226 MB) y corre 100% local, sin API keys.
- **Modelo de lenguaje**: se intentan en orden los proveedores configurados
  (Groq → OpenRouter → Gemini) con *fallback* automático si uno falla o está
  limitado por tasa.

## Requisitos

- Python 3.12+ (el proyecto se desarrolló con 3.14)
- API key de al menos un proveedor (ver abajo)

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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
| `TELEGRAM_WEBHOOK_SECRET`| Secreto para validar el webhook de Telegram   | Generar uno (o `run_bot.sh` lo crea)   |

Los proveedores sin key se ignoran automáticamente. Con una sola key el chat
funciona; con varias, si el primero falla se pasa al siguiente.

## Cargar los datos del negocio (la única fuente de información)

```bash
# Inicia el servidor (el puerto 8000 lo usa el document-service)
uvicorn main:app --reload --port 8001

# Sube tu archivo (.txt o .pdf). Reemplaza el ejemplo por tus datos reales.
curl -X POST http://127.0.0.1:8001/documents/upload \
  -F "file=@data/business_data.txt"
```

Hay un documento de ejemplo en `data/business_data.txt` (una panadería ficticia)
para probar. El contenido que indexes será lo único que el chatbot conozca.

> Si reemplazas los datos del negocio, borra y vuelve a crear la colección:
> `rm -rf chroma_db/` y vuelve a subir el documento.

## API

| Método | Ruta                          | Descripción                                   |
| ------ | ----------------------------- | --------------------------------------------- |
| POST   | `/chat`                       | Envía una pregunta y recibe la respuesta       |
| POST   | `/documents/upload`           | Sube e indexa un `.txt` o `.pdf`               |
| GET    | `/documents`                  | Lista los documentos indexados                 |
| DELETE | `/documents/{source_name}`    | Elimina un documento del índice                |
| POST   | `/documents/quote`            | Genera una cotización PDF vía document-service |
| GET    | `/health`                     | Estado del servidor, providers y documentos    |
| POST   | `/webhooks/telegram`          | Webhook que recibe los mensajes de Telegram    |
| GET    | `/webhooks/whatsapp`          | Verificación del webhook de WhatsApp (Meta)    |
| POST   | `/webhooks/whatsapp`          | Webhook que recibe los mensajes de WhatsApp    |

Ejemplo de chat:

```bash
curl -X POST http://127.0.0.1:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "¿Cuánto cuesta el pan de masa madre?"}'
```

La respuesta incluye el `provider` y el `model` usados, y si se usó contexto RAG.

El campo opcional `conversation_id` activa memoria de la conversación (últimos
mensajes del hilo): el bot evita repetir datos que ya dio antes (p. ej. el
contacto) y solo los retoma si el usuario los pide o se despide. Sin ese campo,
cada pregunta es independiente.

### Generar una cotización (PDF)

`/documents/quote` delega en el microservicio `document-service` (requiere
`DOCUMENT_SERVICE_URL` e `INTERNAL_API_KEY` configurados):

```bash
curl -X POST http://127.0.0.1:8001/documents/quote \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "req-001",
    "tenant_id": "pymes",
    "customer": {"name": "Juan Pérez", "phone": "809-555-0101"},
    "items": [{"description": "Consulta legal", "quantity": 1, "unit_price": 2500.0}],
    "currency": "DOP"
  }'
```

Devuelve `{document_id, quote_number, pdf_url, size_bytes, status}`. Si el
document-service no está disponible o rechaza la petición, responde `502`.

### Envío de correos (email-service)

`email_service.py` es un cliente del microservicio `email-service`, que entrega
los correos con Resend. **Todavía no lo invoca ningún flujo del bot**: está
escrito y verificado contra el servicio real, pero desconectado a propósito.

```python
import email_service

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
./run_bot.sh start

# Detener
./run_bot.sh stop

# Re-registrar el webhook si cambió la URL del túnel (por un reinicio)
./run_bot.sh webhook
```

`run_bot.sh` genera `TELEGRAM_WEBHOOK_SECRET` automáticamente si no existe y lo
guarda en `.env`. Abre tu bot en Telegram, pulsa *Start* y escribe.

> La URL gratuita de `trycloudflare.com` cambia en cada reinicio del túnel;
> vuelve a ejecutar `./run_bot.sh webhook` (o `start`) para re-registrarla.
> La memoria de conversaciones es en memoria: un reinicio del servidor equivale
> a una sesión nueva.

## WhatsApp Business (Cloud API de Meta)

Conecta el chatbot a tu número de WhatsApp del negocio usando la Cloud API de
Meta. Como no tienes dominio aún, `run_whatsapp.sh` abre un túnel HTTPS de
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
./run_whatsapp.sh start
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
> reinicio; si reinicias, vuelve a ejecutar `./run_whatsapp.sh start` y repite
> el paso 2 con la nueva URL.

## Estructura

```
main.py               # API FastAPI: chat, subida/gestión de documentos, health, webhooks
rag.py                # Ingesta (troceado), indexado y recuperación en ChromaDB
embeddings.py         # Embedding function multilingüe (E5-small en ONNX)
telegram_adapter.py   # Helpers de la Bot API de Telegram (webhook, formato Markdown)
whatsapp_adapter.py   # Helpers de la Cloud API de WhatsApp/Meta (firma, mensajes)
run_bot.sh            # Levanta uvicorn + túnel Cloudflare y registra el webhook de Telegram
run_whatsapp.sh       # Levanta uvicorn + túnel Cloudflare (webhook de WhatsApp se pega en Meta)
data/                 # Documentos del negocio (fuente de información)
```

## Limitaciones conocidas

- Sin API key, `/chat` devuelve 503 hasta que configures al menos una.
- La velocidad de indexado/búsqueda depende de tu CPU (los embeddings corren en
  local). En hardware modesto cada búsqueda tarda unos segundos.
- `chroma_db/` es la base vectorial regenerable: está en `.gitignore`.