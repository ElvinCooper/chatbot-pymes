# Auditoría técnica: medición de uso y rentabilidad por tenant

> **Estado**: auditoría SOLO LECTURA. Este documento describe el estado actual del
> sistema y propone cómo medir uso/costo por tenant para reportar rentabilidad.
> No se ha modificado código ni configuración: solo se ha inspeccionado y leído.
> Versión del código auditado: `main` en `b015f5e` (commit del 2026).

---

## 1. Resumen ejecutivo

El chatbot es una aplicación FastAPI **plana** (sin paquetes, sin tests, sin CI)
con cuatro canales de entrada (Telegram, WhatsApp, Messenger e Instagram) y un
flujo específico de **cotizaciones en PDF** delegado en un microservicio externo
(`document-service`). No tiene:

- **Multi-tenancy real**: un único tenant hardcodeado (`QUOTE_TENANT_ID = "chatbot"`) en `app/main.py`.
- **Medición de uso alguna**: ni por tenant, ni por conversación, ni por canal.
- **Medición de tokens LLM**: `try_provider` (`app/main.py`) descarta `completion.usage` y devuelve solo el texto.
- **Base de datos de negocio**: el único almacén es ChromaDB (`chroma_db/`) para RAG + dos JSON en `logs/` para deduplicación. El estado de conversación vive en memoria.
- **Contrato de email**: `app/integrations/email.py` existe y está verificado, pero **ningún flujo lo llama**.
- **Costeo WhatsApp**: no existe ningún cálculo, ni parámetros de tarifas, ni detección de país.
- **Panel de administración / reportes**: no existen.
- **Observabilidad**: `logging.basicConfig(level=logging.INFO)` a stdout; sin request-ids, sin métricas.
- **Autenticación**: los endpoints `/chat` y `/documents/*` no requieren API key.

### Conclusión

Para poder reportar **uso y rentabilidad por tenant** hay que añadir una capa de
medición que hoy no existe. Los puntos de integración son pocos y bien
localizados (`app/integrations/channels/whatsapp.py::send_message/send_document`,
`app/main.py::try_provider`/`chat`/`_handle_*_message`), lo que hace la
implementación viable en ~6 fases. Las tarifas (Meta, LLM, infra) **no deben
hardcodearse**: deben vivir en configuración/BD para mantenerse sin tocar código.

---

## 2. Arquitectura actual (ASCII)

```
                       ┌──────────────────────────────────────────────────────┐
                       │                     chatbot-pymes                     │
                       │                       (FastAPI)                       │
                       │                      puertos por canal:              │
                       │       8001 Telegram · 8002 WhatsApp · 8004 Messenger  │
                       │                                                       │
  Telegram ──────────► │  /webhooks/telegram    → _handle_telegram_message      │
  WhatsApp  ─────────► │  /webhooks/whatsapp    → _handle_whatsapp_message      │
  Messenger ─────────► │  /webhooks/messenger   → _handle_messenger_message     │
  Instagram ─────────► │  /webhooks/instagram   → SOLO loguea (no responde)     │
                       │                                                       │
                       │  /chat                 → rag + PROVIDERS (LLM)         │
                       │  /documents/upload|list|delete → rag (ChromaDB)       │
                       │  /documents/quote      → document_service (PDF)       │
                       │  /health                                                  │
                       └──────┬───────────┬──────────────┬────────────┬─────────┘
                              │           │              │            │
                     app/rag/indexing.py   │           │  PROVIDERS   │            │
                app/rag/embeddings.py │           │  (Groq →     │            │
                (E5 local)     │           │  OpenRouter →│            │
                    │          │           │  Gemini)     │            │
              chroma_db/       │           │              ▼            │
                              │           │        [LLM externo]       │
                              ▼           ▼                           ▼
                        ChromaDB      document-service          email-service
                        (RAG)          :8000 (PDF quotes)       :8003 (email)
                                       X-Internal-API-Key        X-API-Key
                                    ─── no contactado por ninguna ruta ───
```

Servicios externos: `document-service` (:8000) genera PDFs de cotización; el
`email-service` (:8003) envía correos pero **no se invoca desde ningún flujo**.
No hay Dockerfile ni docker-compose en el repo (los `.env.example` mencionan
nombres de servicio Docker, pero el orquestador no está versionado aquí).

---

## 3. Estado de la multi-tenancy

| Verificación | Resultado |
|---|---|
| ¿Existe lógica de tenants en el código? | **No.** `QUOTE_TENANT_ID = "chatbot"` (`app/main.py`) es una constante; `document_service.create_quote` y `/documents/quote` reciben `request.tenant_id`, pero el chatbot solo pasa `"chatbot"` |
| ¿`app/integrations/email.py` está preparado? | Sí, parcialmente: su contrato ya incluye `tenant_id` en el payload (`app/integrations/email.py`) y en `EmailResult` |
| ¿Los `conversation_id` codifican tenant? | No: `whatsapp:<wa_id>`, `messenger:<psid>`, `telegram:<chat_id>` — solo canal + id del usuario |
| Plan multitenant | `PLAN_MULTITENANT.md` es un **plan no implementado** (multitenencia por número de WhatsApp). No asumir que existe |

**Implicación**: cualquier reporte por tenant hoy solo puede tener un tenant
(`chatbot`). La medición debe diseñarse *tenant-aware* desde el día 1 (columna
`tenant_id` en todos los registros) aunque hoy solo haya un valor.

---

## 4. Puntos de medición de WhatsApp

### 4.1 Entrada (mensajes del cliente)

`whatsapp_adapter.parse_messages` (líneas 63-103 aprox.) devuelve solo `(wamid, wa_id, text)`:

- **`wa_id`** = destinatario del cliente (identifica al contacto). Disponible.
- **`wamid`** = id del mensaje (usado para dedup, `app/main.py`, persistido en `logs/recent_wamids.json`).
- **`timestamp`** NO se propaga (solo se usa para descartar mensajes viejos > 900 s, `STALE_MESSAGE_SECONDS`).
- **`phone_number_id`** (el número de la empresa que recibió) NO se lee — es la pieza que permitiría distinguir tenants/números en multitenancy.
- **`value.statuses[]`** nunca se itera → **no hay receipts de entrega/lectura/fallo** del canal. La lectura es la señal que Meta usa para cosas como límites de conversación por usuario, y es gratis de capturar.

Punto de inyección: el bucle de `_handle_whatsapp_message` (`app/main.py`) ya recibe `(wamid, wa_id, message)`; ahí se conoce `conversation_id` y destino.

### 4.2 Salida (respuestas del bot)

- `send_message(wa_id, text)` (`app/integrations/channels/whatsapp.py`): hace `POST .../{phone_number_id}/messages` y **descarta la respuesta de Meta** → no se captura el **wamid de salida** ni la categoría de la respuesta.
- `send_document(wa_id, file_bytes, filename, caption)` (`app/integrations/channels/whatsapp.py`): sube media → `media_id` → envía documento. Tampoco captura el wamid de salida.
- Ambos devuelven `None` y solo loguean con `logger.warning` los fallos (HTTP != 200).

Punto de inyección: cambiar el retorno de estas dos funciones para que devuelvan el `wamid`/id de respuesta (o `None`), y envolverlas en `_handle_whatsapp_message`.

### 4.3 Categorización y free-tier

No existe. No hay parámetro de `free_tier` de WhatsApp Business, ni contador de conversaciones dentro de la ventana gratuita, ni envíos iniciados por empresa (templates fuera de las 24 h). Ver modelo propuesto en §11.

---

## 5. Medición de uso de LLM

### 5.1 Llamadas al LLM (dos rutas)

1. **`/chat`** (`app/main.py`): recupera contexto RAG, arma mensajes y recorre `PROVIDERS`.
2. **Extractor de cotizaciones** (`extract_quote_draft`, `app/main.py` ~590-655): usa el mismo `Provider`, con prompt JSON (`QUOTE_EXTRACTION_PROMPT`) y hasta 2 intentos si hay señales de cotización (`app/main.py`).

### 5.2 Proveedores (hardcodeados)

`PROVIDERS` (`app/main.py`), orden = prioridad:

| Prioridad | Provider | Modelo | Env key |
|---|---|---|---|
| 1 | groq | `openai/gpt-oss-120b` | `GROQ_API_KEY` |
| 2 | openrouter | `meta-llama/llama-3.3-70b-instruct` | `OPENROUTER_API_KEY` |
| 3 | gemini | `gemini-3.6-flash` | `GEMINI_API_KEY` |

Fallback automático en `chat` (`app/main.py`): ante `RateLimitError/APITimeoutError/APIError/ValueError` pasa al siguiente. Los modelos y el orden están **hardcodeados en el código**, no en env.

### 5.3 Tokens: NO se miden

`try_provider` (`app/main.py`):

```python
async def try_provider(provider: Provider, messages: list[dict]) -> str:
    if not provider.api_key:
        raise ValueError(f"{provider.name}: falta API key")
    client = AsyncOpenAI(base_url=provider.base_url, api_key=provider.api_key)
    completion = await client.chat.completions.create(
        model=provider.model, messages=messages, timeout=15,
    )
    return completion.choices[0].message.content or ""
```

- `completion.usage` (con `prompt_tokens` / `completion_tokens` / `total_tokens`) **se descarta**.
- No se registra ni provider que respondió, ni modelo, ni tokens en el log (solo `logger.info("Respondido por %s (%s)")`, `app/main.py`).
- La ruta de cotización tampoco mide tokens; `extract_quote_draft` loguea `provider.name`/`model` de quien extrajo (`app/main.py`).

**Implicación**: sin `usage`, no hay base de datos real de consumo → sin costo LLM por tenant. Es la pieza central para el reporte de rentabilidad.

---

## 6. Estado del correo electrónico (email)

- `app/integrations/email.py` es un cliente del microservicio `email-service` (:8003):
  - `POST {EMAIL_SERVICE_URL}/api/v1/emails/send` con cabecera `X-API-Key` (`API_KEY_HEADER`).
  - **`Idempotency-Key` obligatoria en la práctica** (`build_idempotency_key(scope, entity_id)`, formato `<tipo-evento>/<id-entidad>`); sin ella un reintento duplica el correo.
  - Payload plano `{tenant_id, to, subject, text?, html?, attachments?, reply_to?, metadata?}`; el remitente lo pone el servicio (`EMAIL_FROM`), **no se manda `from`** (el esquema usa `extra="forbid"` y daría 422 `INVALID_REQUEST`).
  - `send_email` devuelve `EmailResult(email_id, tenant_id)` o `None` (loggeado); nunca propaga excepciones.
  - Adjuntos en Base64 en memoria (`encode_attachment`); límites: 10 adjuntos, 10 MB c/u, 25 MB en total.
  - `email_id` va en el **nivel superior** de la respuesta, no dentro de `data`; hay que comprobar `success` antes de leer el id.
- **Cero llamadas**: `grep` confirma que ningún flujo invoca `send_email`. El módulo está "listo y verificado, sin uso".
- La `Idempotency-Key` caduca a las 24 h en Resend: misma clave + cuerpo distinto → `409 invalid_idempotent_request` → `502 RESEND_SEND_FAILED` → `None`.

**Implicación para métricas**: cuando se conecte un flujo (p. ej. envío de la ficha de cotización por email), ya existe `tenant_id` y `email_id` para registrar uso. El costo del envío vive en Resend (fuera de este repo).

---

## 7. Estado de la base de datos

| Almacén | Qué guarda | Persistencia |
|---|---|---|
| ChromaDB (`chroma_db/`) | Índice RAG de documentos del negocio (1 fuente actual: `data/business_data.txt`, 17 chunks) | Sí (disco) |
| `logs/recent_wamids.json` | Dedup de updates de WhatsApp (wamid → epoch), TTL 24 h, tope 1000 | Sí (disco), gitignored |
| `logs/recent_mids.json` | Dedup de Messenger (mid → epoch), TTL 24 h, tope 1000 | Sí (disco), gitignored |
| Memoria del proceso | `_conv_histories` (tope 8), `pending_quote_info`, `_last_quote_pdf`, `_idle_last_activity`, `_seen_update_ids` (Telegram, en memoria) | No (se pierde al reiniciar) |

**No hay**: SQL, ORM, migraciones, Postgres/MySQL/SQLite, ni base de métricas.
El estado en memoria tiene un patrón útil que replicar para metería: JSON con TTL + tope + persistencia en `logs/` (ver `_load/_save_recent_wamids`/`_save_recent_mids`, `app/main.py`).

Reinicio del proceso = se pierde el historial de conversación y los PDFs, pero **no** el dedup de WhatsApp/Messenger.

---

## 8. Arquitectura futura propuesta

```
                       ┌──────────────────────────────────────────────────────────┐
                       │                        chatbot-pymes                      │
                       │                                                          │
  Telegram ──────────► │ webhooks ──► _handle_*_message                           │
  WhatsApp  ─────────► │                │                                         │
  Messenger ─────────► │                ├──> /chat (RAG + LLM)                    │
  Instagram ─────────► │                ├──> flujo de cotización (PDF)            │
                       │                └──> TELÉMETRO (nuevo)                    │
                       │                        │                                 │
                       │   /chat ── LLM ──► USAGE: tokens, provider, modelo       │
                       │   send_message ─► USAGE: canal, id salida, categoría     │
                       │                       │                                  │
                       └───────────────────────┼──────────────────────────────────┘
                                               ▼
                              ┌──────────────────────────────┐
                              │   SQLite (SQLAlchemy)        │  ← nueva BD de uso
                              │   usage_events (granular)   │     (evolucionable a
                              │   aggregates (diarios)      │      Postgres después)
                              │   tenant/rate_config        │
                              └──────────────┬───────────────┘
                                             │
          ┌────────────────────┬─────────────┼──────────────────┬─────────────────┐
          ▼                    ▼             ▼                  ▼                 ▼
   WhatsApp (Meta)        LLM externo     email-service      document-service  (infra local:
    - mensajes            - tokens/prices  - envíos           - PDFs generados    VPS/Cloudflare)
    - free_tier / tarifa  (config, no    (tenants + idemp.)                        (costo fijo)
    - país por prefijo     hardcodeado)
```

Principios de la arquitectura propuesta:

1. **Eventos de uso atómicos antes que agregados**: guardar un registro por llamada LLM y por mensaje de canal; los agregados diarios se calculan después (baratos sobre SQLite).
2. **Diseño tenant-aware desde el inicio**: columna `tenant_id` en todo, aunque hoy solo exista `chatbot`.
3. **Tarifas en configuración/BD, nunca en código**: precios de Meta y de LLM como datos parametrizables, no constantes.
4. **No bloquear el hot path**: la escritura de métricas debe ser asíncrona/barata (append a SQLite en background task o cola simple) para no añadir latencia al chat.
5. **Sin dependencias nuevas pesadas**: SQLAlchemy (o incluso `sqlite3` estándar) y una librería de scripting para el panel inicial son suficientes; un dashboard se puede empezar con un script de consola + CSV.

---

## 9. Modelo de datos propuesto

Tablas (SQLite vía SQLAlchemy, nombres en plural):

### `tenants`
| columna | tipo | nota |
|---|---|---|
| id | str PK | hereda del valor de env `TENANT_ID` (hoy `chatbot`) |
| name | str | nombre comercial |
| default_currency | str (3) | `DOP` por defecto |
| created_at | datetime | |

### `usage_events` (eventos atómicos, append-only)
| columna | tipo | nota |
|---|---|---|
| id | int PK autoincrement | |
| event_time | datetime | índice por evento |
| tenant_id | str FK | `chatbot` hoy |
| channel | str | `whatsapp`/`messenger`/`telegram`/`instagram`/`api` |
| direction | str | `inbound`/`outbound` |
| event_type | str | `chat_reply`, `quote_extraction`, `quote_pdf`, `message_sent`, `message_received`, `email_sent`, `email_oob`… |
| conversation_id | str | `whatsapp:<wa_id>`… |
| contact_id | str | `wa_id`/`psid`/`chat_id` |
| provider | str | `groq`/`openrouter`/`gemini`/`-` |
| model | str | modelo usado |
| prompt_tokens | int | 0 si no aplica |
| completion_tokens | int | 0 si no aplica |
| total_tokens | int | = prompt + completion |
| wamid | str | id de mensaje (inbound o outbound) |
| payload_size_bytes | int | para PDFs/correos |
| category | str | categoría WhatsApp (ver §11) |
| country_code | str (2) | `DO`, `US`… (prefijo del `wa_id`) o `-` |
| free_tier_applied | bool | si el evento consumió conversación free |
| billable_quantity | int | número de unidades facturables (ver §11) |
| unit_price | float | precio por unidad en ese momento (copiado de rate_config al escribir el evento, para no depender de cambios futuros de tarifa) |
| cost_amount | float | billable_quantity × unit_price (USD canónico) |
| metadata | json | blob libre (`statuses[]`, errores, etc.) |
| created_at | datetime | |

### `daily_aggregates` (resumen calculado, por tenant+canal+modelo)
| columna | tipo |
|---|---|
| tenant_id | str FK |
| day | date |
| channel | str |
| provider / model | str |
| messages_in / messages_out | int |
| quote_pdfs | int |
| emails_sent | int |
| prompt_tokens / completion_tokens / total_tokens | int |
| whatsapp_conversations_free / billable | int |
| cost_native_currency | float | la que corresponda a cada item |
| cost_usd | float | |

### `rate_config` (tarifas parametrizadas, NO hardcodeadas)
| columna | tipo | nota |
|---|---|---|
| scope | str PK | `whatsapp:<country_code>`, `llm:<provider>:<model>` |
| unit_price | float | USD por unidad |
| currency | str | `USD` |
| effective_from / effective_to | date | historial de precios (los eventos guardan una copia) |
| notes | str | |

### `tenant_settings` (opcional)
`free_tier_enabled`, `send_closing_sms`, `daily_email_report`, `notify_email`.

> La clave de idempotencia del email (`build_idempotency_key`) ya provee el id
> estable (`quote-email/quote-...`) para deduplicar el evento de email; reutilízala
> para el evento de uso y evita duplicados en métricas con `(tenant_id, event_type,
> idempotency_key)` único.

---

## 10. Puntos de integración exactos (por archivo y función)

| Archivo | Función / línea | Contrato hoy | Qué hay que añadir |
|---|---|---|---|
| `app/main.py` | `try_provider` (`:352-362`) | devuelve `str` (solo texto) | devolver también `usage` y `model`; registrar evento `chat_reply`/`quote_extraction` con tokens, provider y modelo |
| `app/main.py` | `chat` (`:419-452`) | arma mensajes y recorre PROVIDERS; devuelve `ChatResponse` | en cada éxito registrar evento LLM (tokens reales) y `provider_used`/`model_used` |
| `app/main.py` | `extract_quote_draft` (~`:590-655`) | intenta extraer JSON de cotización | registrar evento `quote_extraction` con tokens + resultado (items/`items:[]`/intent no) |
| `app/main.py` | `materialize_quote` / `create_quote` (~`:660-700`) | genera PDF en document-service y lo descarga | registrar evento `quote_pdf` con `size_bytes`, éxito/fallo |
| `app/main.py` | `_handle_whatsapp_message` (`:1054-1080`) | orquesta chat/cotización y envía | envolver `send_message`/`send_document` para capturar resultados; registrar evento inbound (con `country_code`, `category`) y outbound |
| `app/main.py` | `whatsapp_webhook` (`:1023-1051`) | dedup por wamid + background | enriquecer el parse con `phone_number_id` y `value.statuses[]` (receipts) |
| `app/integrations/channels/whatsapp.py` | `parse_messages` (`:63-103`) | solo devuelve `(wamid, wa_id, text)` | devolver también timestamp y `phone_number_id` (o un dict) |
| `app/integrations/channels/whatsapp.py` | `send_message` (`:120-149`) / `send_document` (`:152-201`) | devuelven `None` | devolver el `wamid`/id de respuesta de Meta (o `None`) |
| `app/integrations/channels/whatsapp.py` | (nuevo) | — | helpers: `detect_country(wa_id)`, `classify_conversation(...)` |
| `app/main.py` | `messenger_adapter` / `_handle_messenger_message` (`:1131-1160`) | igual que WhatsApp, `fallback_phone=""` | registrar eventos inbound/outbound (sin categoría o con categoría `-`) |
| `app/main.py` | `telegram` (`:951-1001`) | mensajes/chat en background | misma medición (sin costos free-tier de Meta; solo eventos) |
| `app/integrations/email.py` | `send_email` (`:79-153`) | implica `tenant_id` + idempotencia, hoy sin callers | cuando un flujo la llame, registrar `email_sent` con `email_id` |
| `app/main.py` | `/chat` (`:419`) y `/documents/*` (`:871-926`) | **sin autenticación** | opcional: proteger/restringir; el tenant de origen debería venir de la cabecera del canal, no de un campo libre |
| `app/integrations/documents.py` | `create_quote` | pide `tenant_id`, hoy siempre `chatbot` | mantener; el tenant vendría del canal/contacto |

---

## 11. Modelo de costos de WhatsApp (propuesto)

**Regla**: NO hardcodear tarifas de Meta en el código. Todo vive en `rate_config`
o en env, actualizable sin desplegar.

### 11.1 Categoría de conversación

En cada evento outbound se determina la categoría según el contexto (almacenada
en `usage_events.category`):

| Categoría | Regla sugerida |
|---|---|
| `service` | respuesta dentro de la ventana de **24 h** del último mensaje entrante del usuario (conversación iniciada por el cliente) |
| `utility` | envío iniciado por la empresa que encaja en una plantilla de utilidad aprobada (p. ej. confirmaciones de cotización) |
| `marketing` / `authentication` | otras plantillas; normalmente fuera de alcance para este bot |

El estado de "último mensaje del usuario" ya se puede derivar del **timestamp
entrante** que hoy se descarta (`STALE_MESSAGE_SECONDS` solo lo usa para filtrar;
la marca de tiempo del último inbound por `conversation_id` es la semilla de la
ventana de 24 h). El monitor de inactividad (`_idle_last_activity`) es un
precedente de bookkeeping por conversación en memoria.

### 11.2 Free tier y cantidad facturable

- `tenant_settings.free_tier_enabled` controla la ventana gratuita (los cambios de
  política de Meta no deben obligar a tocar código).
- Por conversación iniciada por el cliente dentro del free tier se reclama
  `billable_quantity = 0` y `free_tier_applied = true`.
- Fuera del free tier / conversaciones billables: `billable_quantity = 1` por
  mensaje de la empresa cobrable (o por conversación, según se defina en
  `rate_config`; la unidad se documenta en `notes`).
- Al escribir el evento se hace **snapshot** de `unit_price` y `country_code`
  (copiados de `rate_config` al momento), igual que hace rápido dedup con TTL.

### 11.3 Detección de país por prefijo

Función propuesta `detect_country(wa_id)` con tabla interna de rangos de prefijos
internacionales → `ISO-3166-1 alpha-2`:

```
+1  → US/CA (pero 1-809 → DO), +52 → MX, +57 → CO, +34 → ES,
+33 → FR, +49 → DE, +54 → AR, +56 → CL, +51 → PE, +58 → VE, ...
```

La tarifa por país se resuelve en `rate_config` como `whatsapp:<country_code>`.
El mapa prefijo→país es configurable (CP1252-free, datos), y los países no
conocidos caen en `rate_config:whatsapp:default`.

> Este proyecto es dominicano (moneda `DOP`, cotillón/pastelería en
> `data/business_data.txt`), así que `+1 8xx` (RD) y la RD deben mapearse bien.

---

## 12. Almacenamiento de precios de modelos de IA

Igual que WhatsApp, **las tarifas de LLM son datos, no código**:

- Fila por `llm:<provider>:<model>` en `rate_config` con `unit_price` por
  token/1M tokens (ej. campos `price_per_1m_prompt` / `price_per_1m_completion`
  o un MVP con un único `unit_price` y `notes`; el modelo de datos de §9 se
  puede ajustar).
- Con el `usage` real de `completion.usage`, el costo por llamada =
  `prompt_tokens × prompt_price_per_1m / 1e6` + `completion_tokens ×
  completion_price_per_1m / 1e6`.
- `effective_from`/`effective_to` permiten conservar el histórico de precios y
  **el evento guarda el snapshot**, así un cambio de tarifa no reescribe el pasado.
- Si un modelo no tiene tarifa conocida, `cost_amount` queda `NULL` y el reporte
  lo marca como "sin costear" (nunca inventar el precio).

---

## 13. Asignación de costos de infraestructura

Costos fijos/mensuales (VPS, Cloudflare, el propio proceso):

| Componente | Costo | Naturaleza |
|---|---|---|
| VPS/hosting del bot + tunnels | fijo mensual | no atribuible a un evento concreto |
| Document-service (:8000) | fijo mensual | se puede atribuir a `quote_pdf` (proporcional) |
| Email-service (:8003) | costo por envío (Resend) | atribuible a cada `email_sent` |
| Embeddings E5 ONNX (~226 MB) | una sola vez + disco | fijo, no por llamada |

Método propuesto: guardar en `tenants`/config un `monthly_infra_cost` (USD) y el
reporte calcula **margen de contribución = ingresos_estimados − (costos
variables LLM + WhatsApp + email + prorrateo de infra)**. Para un solo tenant el
prorrateo es trivial (100 %); con N tenants se repartiría por uso relativo
(tokens, mensajes) o por cabecera configurada.

---

## 14. Panel de administración y reportes

**No existe hoy.** Propuesta en 3 niveles, en orden de esfuerzo:

1. **CLI de reporte (MVP)**: script `scripts/report.py` que lea SQLite y
   imprima/exporte CSV: uso diario por tenant/canal/modelo, tokens, costo LLM,
   mensajes WhatsApp (free vs billable por país), cotizaciones y emails.
2. **Endpoint interno `/admin/usage`** protegido con API key (o token de lectura)
   que devuelva los mismos agregados en JSON — sin UI todavía.
3. **Dashboard simple** (opcional): exportación HTML estático por día, o
   integración ligera si más adelante se añade el email-service como canal de
   entrega (el propio bot puede mandar el resumen diario por correo).

El **destino natural del reporte diario** es el email-service con
`Idempotency-Key = f"usage-report/{tenant_id}/{YYYY-MM-DD}"` → misma clave para
el mismo informe, distinta por día (evita duplicados en reintentos).

---

## 15. Observabilidad y correlación

| Necesidad | Estado actual | Propuesta |
|---|---|---|
| Request id / correlación | No existe | un `request_id` por update de webhook (wamid/mid/update_id ya sirven) y por llamada `/chat`; propagarlo en logs |
| Logs estructurados | `logging.basicConfig(INFO)` stdout, mensajes ad-hoc | pasar a `logging` con formato clave=valor o JSON por evento, con `tenant_id`/`conversation_id` |
| Métricas de negocio | No | provenir de `usage_events`, no de los logs |
| Métricas de canal | Solo dedup | aprovechar `value.statuses[]` (WhatsApp) para fallos de entrega y contar en `metadata` |
| Alertas | No | “sin provider disponible” (503, `app/main.py`) o “send_message falló” (`app/integrations/channels/whatsapp.py`) → evento con tipo `error` en `usage_events.metadata` |
| Trazado del flujo de cotización | Solo `logger.warning`; fallos silenciosos (`materialize_quote` → `None` → `QUOTE_PENDING_MSG`) | evento `quote_pdf` con `metadata.failure_reason` |

---

## 16. Riesgos

1. **Tokens sin medir** (alta): sin `usage`, imposible costear LLM. Es el cambio de menor esfuerzo y mayor impacto.
2. **`send_message`/`send_document` descartan la respuesta de Meta** (alta): sin el wamid de salida no se puede casar respuesta↔costo ni clasificar.
3. **La categoría WhatsApp puede equivocarse** (media): si se clasifica mal entre `service`/`utility`/`marketing`, el snapshot de `unit_price` queda mal grabado. Mitigar: guardar la categoría bruta + la heurística de `is_oob` y permitir re-clasificación.
4. **Free tier cambia con la política de Meta** (media): por eso es configuración, nunca constante.
5. **Detección de país imprecisa por prefijos móviles** (baja-media): los rangos por prefijo tienen excepciones (`+1` vs `+1 8xx`); mantener la tabla configurable y un `:default`.
6. **Latencia añadida en el hot path** (media): escribir métricas en `BackgroundTasks`/cola; nunca en línea dentro de la respuesta del webhook (que debe devolver 200 rápido).
7. **Waids perdidos en `BackgroundTasks`** (media): si el proceso muere tras aceptar el update y antes de responder, no hay evento outbound. Aceptable al principio; documentarlo.
8. **Doble conteo por dedup si se persiste mal** (media): reutilizar el mismo mecanismo wamid/mid; el evento debe escribirse **después** del check de duplicado.
9. **Sin tests ni CI** (media en esta auditoría): al añadir métricas hay que introducir al menos tests unitarios del costeo y del clasificador; hoy no existe suite (ver §18).
10. **Endpoints sin autenticación** (media): `/chat` y `/documents/*` son llamables por cualquiera que alcance el puerto; aislar por red o añadir API key antes de exponer métricas.
11. **Limitar `logs/`**: `recent_wamids.json` y `recent_mids.json` ya crecen (TTL 24 h, tope 1000); `usage_events` debe podar (retener N días configurables) para no llenar el disco.

---

## 17. Fases de implementación

> Sin escribir código todavía: estas son las fases **propuestas** para cuando se
> apruebe implementar. Cada fase es independiente y desplegable.

### Fase 1 — Captura de tokens LLM (mayor impacto, menor esfuerzo)
- `try_provider` devuelve `(text, usage, model)`; `chat` y `extract_quote_draft` registran eventos con `prompt/completion/total_tokens`, provider y modelo.
- Persistencia mínima: tablas `usage_events` (registros LLM) en SQLite durante esta fase.

### Fase 2 — Badge de canal / eventos inbound–outbound
- `whatsapp_adapter.parse_messages` devuelve `(wamid, wa_id, timestamp, phone_number_id, text)`.
- `send_message`/`send_document` devuelven el id de respuesta de Meta.
- `_handle_*_message` registra inbound/outbound con `conversation_id` y `contact_id`.
- Leer `value.statuses[]` y registrar receipts (fallos de entrega) en `metadata`.

### Fase 3 — Costeo configurável (WhatsApp + LLM)
- Tablas `rate_config`/`tenant_settings`; `detect_country(wa_id)`; clasificación `service`/`utility` + ventana 24 h; snapshot unit_price/país en el evento.

### Fase 4 — Email conectado al flujo (si procede)
- Conectar `send_email` en el flujo de cotización (o en el reporte diario), con `Idempotency-Key` estable y evento `email_sent` con `email_id`.

### Fase 5 — Agregados y reporte
- `daily_aggregates` calculado desde `usage_events` (job o a demanda al facturar).
- `scripts/report.py` (CLI → CSV/console) y endpoint `/admin/usage` con token de lectura.
- Margen de contribución = ingresos estimados − (LLM + WhatsApp + email + prorrateo infra).

### Fase 6 — Panel / entrega del reporte
- Dashboard estático simple o envío del resumen diario por email (`Idempotency-Key` por día).
- Retención/poda de `usage_events`.

---

## Archivos a modificar (cuando se implemente)

| Archivo | Cambio |
|---|---|
| `app/main.py` | `try_provider` (devolver usage/model); `chat` y `extract_quote_draft` (registrar evento); `whatsapp_webhook` (parse enriquecido + statuses); `_handle_*_message` (eventos inbound/outbound); config del tenant desde env `TENANT_ID` (hoy `chatbot`) |
| `app/integrations/channels/whatsapp.py` | `parse_messages` (timestamp + `phone_number_id`); `send_message`/`send_document` (devolver wamid); helpers `detect_country`/clasificador |
| `app/integrations/email.py` | sin cambios funcionales: ya es tenant-aware; solo añadir caller en el flujo que corresponda |
| `.env.example` | nuevas variables: `METRICS_DB_PATH`, `TENANT_ID`, `FREE_TIER_ENABLED`, `ADMIN_TOKEN`, `REPORT_EMAIL_TO`, `INFRA_COST_MONTHLY_USD` |
| `requirements.txt` | `sqlalchemy` (o usar `sqlite3` stdlib para evitar dependencia) |
| `scripts/run_bot.sh` / `scripts/run_whatsapp.sh` / `scripts/run_messenger.sh` | nada obligatorio; solo si se expone el endpoint `/admin/usage` |

### Archivos nuevos (cuando se implemente)

| Archivo | Propósito |
|---|---|
| `app/persistence/metrics.py` | modelo SQLite (SQLAlchemy o sqlite3), funciones `record_event`, `daily_aggregates`, `retention` |
| `rate_config.py` | tabla `rate_config` + carga desde env/BD; `detect_country`; clasificador WhatsApp |
| `scripts/report.py` | CLI de reporte (CSV/consola) |
| `scripts/seed_rates.py` | siembra de `rate_config` y `tenant_settings` (sin valores inventados; placeholders) |
| `tests/test_metrics.py`, `tests/test_country.py`, `tests/test_quoting_pricing.py` | primeros tests (ver §18) |
| `metrics.sqlite3` (runtime, gitignored) | BD de uso |

### Dependencias

- Obligatoria: ninguna nueva si se usa `sqlite3` de la stdlib (recomendado para el MVP).
- Opcional: `sqlalchemy` (comodidad), algo de dateutil si se necesita parseo de fechas de rate_config.
- Prohibido introducir: estructuras pesadas (Kafka/Redis/Postgres) para el MVP; el volumen es de cientos de eventos/día.
- Sin nuevas claves externas: las tarifas van en config/BD, no en `.env`.

### Pruebas

No existe suite hoy (sin `tests/`, sin `pyproject.toml`, sin CI). Al implementar:

- Clasificación WhatsApp: `service` dentro de ventana 24 h vs `utility`/`oob`.
- `detect_country`: casos `+1 809` → DO, `+52`, `+1 202` → US, prefijo desconocido → default.
- Costeo LLM: snapshot correcto con `rate_config` y `price_per_1m`.
- Idempotencia del evento de email: misma `Idempotency-Key` → un solo evento.
- Dedup: un wamid duplicado (retry de Meta) no duplica el evento.
- Frame de retención de `usage_events` y correcto pruning de `logs/*.json`.

---

## Recomendaciones finales

1. **Empezar por la Fase 1** (tokens LLM): es el cambio con mejores razón impacto/esfuerzo y desbloquea el resto del costeo.
2. **No hardcodear nunca una tarifa** (Meta ni LLM): todo en `rate_config`/config con snapshot en el evento.
3. **Guardar el `country_code` y la `category` en el momento del evento** (snapshot), nunca resolver la tarifa retroactivamente.
4. **Reutilizar la idempotencia existente** (wamid/mid/`Idempotency-Key`) como clave natural de los eventos de uso para no duplicar conteos.
5. **Mantener el hot path rápido**: los eventos se encolan en `BackgroundTasks`, no se escriben dentro del ciclo 200 del webhook.
6. **Añadir auth mínima a `/admin/usage`** y considerar aislar `/chat` y `/documents/*` de la red pública antes de exponer métricas.
7. **Retención configurables** para `usage_events` (p. ej. 90 días) y mantener la poda de `logs/*.json`.
8. **Contratar el reporte diario por email como primera entrega visible**: demuestra rentabilidad por tenant aprovechando el email-service ya construido, con `Idempotency-Key` diaria.

> Este documento es el entregable de la auditoría. No introduce cambios en el
> código: todo lo anterior es diagnóstico y propuesta pendiente de aprobación.
