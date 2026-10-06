# Plan: Multitenancy WhatsApp por número (1 número = 1 negocio)

> Estado: pendiente de revisión. No implementado.
> Decisiones tomadas: config en `tenants.json`, 1 número = 1 negocio con su propia base de conocimiento y prompt, alcance Fase 1 completa.

## Contexto

El transporte ya recibe los mensajes de todos los números del WABA en un único webhook
(con `value.metadata.phone_number_id` indicando qué número recibió), pero el código lo
descarta y asume un solo número/negocio por proceso.

Bloqueos actuales:

1. `parse_messages` (`whatsapp_adapter.py:58-88`) devuelve `(wamid, wa_id, texto)` sin
   leer `value.metadata`; el webhook (`main.py:631-655`) solo propaga `wa_id`.
2. `send_message`/`send_document` (`whatsapp_adapter.py:105-186`) usan un único
   `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` de env (singleton).
3. Colisión de memoria entre tenants: `_conv_histories`, `_last_quote_pdf`, `_idle_*`
   se indexan por `conversation_id = "whatsapp:<wa_id>"` (`whatsapp_adapter.py:42-44`).
   El mismo cliente escribiendo a dos números mezcla conversaciones. `_recent_wamids`
   es un set global con `clear()` global (`main.py:645-646`), frágil multi-tenant.
4. RAG de un solo negocio: colección única `business_docs`; `retrieve_context` no
   recibe tenant; `/documents/*` globales.
5. `BASE_SYSTEM_PROMPT` única y `QUOTE_TENANT_ID = "chatbot"` hardcodeado
   (`main.py:95-105`, `main.py:249`).

A favor: `conversation_id` es string opaca (fácil de prefijar), ChromaDB filtra por
`where` sobre metadata, `create_quote` ya recibe `tenant_id`, `wamid` es único global.

## Cambios

### 1. Nuevo `tenants.py`

- Dataclass `Tenant`: `phone_number_id`, `access_token`, `tenant_id`, `name`, `prompt` (opcional).
- `load_tenants()`: lee `tenants.json` al importar (sin hot reload). Si no existe →
  modo legado: un tenant con `WHATSAPP_PHONE_NUMBER_ID` + `WHATSAPP_ACCESS_TOKEN` de env.
- `get_tenant(phone_number_id) -> Tenant | None`.
- `build_tenant_prompt(tenant)`: si no hay `prompt` custom → "Eres el asistente virtual
  de {name}. " + prompt base.
- `tenants.json.example` versionado; `tenants.json` en `.gitignore`.

```json
{
  "tenants": {
    "1306970875840540": {
      "tenant_id": "panaderia-elsol",
      "name": "Panadería El Sol",
      "access_token": "<per-WABA/system-user>",
      "prompt": "Eres el asistente de Panadería El Sol..."
    }
  }
}
```

### 2. `whatsapp_adapter.py`

- `parse_messages` → 4-tuplas `(wamid, wa_id, phone_number_id, texto)`, leyendo
  `value.metadata.phone_number_id`.
- `send_message`/`send_document` reciben el `Tenant` (token+pnid) en vez de leer env.
- `WHATSAPP_VERIFY_TOKEN`/`WHATSAPP_APP_SECRET` quedan globales (handshake y firma son
  por App, no por número).

### 3. `rag.py` — RAG por tenant (colección única + filtro `where`)

- Metadata de chunk + `tenant` en `ingest_document`.
- `retrieve_context(query, tenant)`, `list_sources(tenant)`, `delete_source(source, tenant)`,
  `ingest_document(..., tenant)` con `where={"tenant": ...}`; `count(where=...)`.
- Tenants `"default"` = legado/telegram/manual.
- Alternativa descartada: colección por tenant (duplica el check de recreate).

### 4. `main.py`

- Webhook: `(wamid, wa_id, pnid, msg)` → `get_tenant(pnid)`; si no está configurado,
  log + ignora. `_handle_whatsapp_message(wamid, wa_id, tenant, msg)`.
- `conversation_id = "whatsapp:<tenant_id>:<wa_id>"` → memoria segura entre tenants.
- `_recent_wamids` → `dict[tenant_id, set]` (reemplaza el `clear()` global).
- Tenante a través del flujo: `_run_quote_flow(..., tenant)` → `retrieve_context` con
  tenant + `tenant_id` real en `document_service.create_quote`. Telegram conserva
  `QUOTE_TENANT_ID`.
- `/documents/upload|list|delete`: parámetro `tenant` (default `"default"`), API admin,
  breaking menor.
- `/chat`: campo opcional `tenant` (default `"default"`) para pruebas; webhooks pasan su tenant.
- `/health`: `whatsapp` → `{"configured": n, "numbers": [tenant_id...]}`.

### 5. Docs

- README: sección multitenant (`tenants.json`, cómo obtener `phone_number_id` del
  webhook, subir datos por tenant).
- AGENTS.md: gotchas de memoria por tenant, `tenants.json` se lee al importar.

## Verificación

- Payload de ejemplo con `metadata.phone_number_id` → parse correcto; 2 tenants en `tenants.json`.
- Servidor: `/health` lista números; mensaje a cada número responde con su prompt/tenant
  (verificar en logs el `tenant_id` en POST a document-service y la colección usada).
- Legado sin `tenants.json` sigue funcionando; número desconocido → ignora sin crash.