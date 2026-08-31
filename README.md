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

| Variable            | Proveedor                                        | Cómo obtener la key                    |
| ------------------- | ------------------------------------------------ | -------------------------------------- |
| `GROQ_API_KEY`      | Groq (rápido y gratuito, recomendado)            | https://console.groq.com/keys          |
| `OPENROUTER_API_KEY`| OpenRouter (muchos modelos, incl. gratis)        | https://openrouter.ai/settings/keys    |
| `GEMINI_API_KEY`    | Google Gemini                                    | https://aistudio.google.com/apikey     |

Los proveedores sin key se ignoran automáticamente. Con una sola key el chat
funciona; con varias, si el primero falla se pasa al siguiente.

## Cargar los datos del negocio (la única fuente de información)

```bash
# Inicia el servidor
uvicorn main:app --reload

# Sube tu archivo (.txt o .pdf). Reemplaza el ejemplo por tus datos reales.
curl -X POST http://127.0.0.1:8000/documents/upload \
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
| GET    | `/health`                     | Estado del servidor, providers y documentos    |

Ejemplo de chat:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "¿Cuánto cuesta el pan de masa madre?"}'
```

La respuesta incluye el `provider` y el `model` usados, y si se usó contexto RAG.

## Estructura

```
main.py          # API FastAPI: chat, subida/gestión de documentos, health
rag.py           # Ingesta (troceado), indexado y recuperación en ChromaDB
embeddings.py    # Embedding function multilingüe (E5-small en ONNX)
data/            # Documentos del negocio (fuente de información)
```

## Limitaciones conocidas

- Sin API key, `/chat` devuelve 503 hasta que configures al menos una.
- La velocidad de indexado/búsqueda depende de tu CPU (los embeddings corren en
  local). En hardware modesto cada búsqueda tarda unos segundos.
- `chroma_db/` es la base vectorial regenerable: está en `.gitignore`.