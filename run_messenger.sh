#!/usr/bin/env bash
#
# Facebook Messenger (Messenger Platform de Meta) en un comando.
#
# Levanta el chatbot (uvicorn) + un túnel HTTPS temporal de Cloudflare para
# que el panel de Meta pueda alcanzar el webhook, sin dominios propios.
#
# Meta NO permite registrar el webhook por API (a diferencia de Telegram):
# la URL y el verify_token se pegan a mano en el panel. Este script solo
# levanta el servidor + el túnel e imprime la URL.
#
# Uso:
#   ./run_messenger.sh start     # levanta todo (predeterminado)
#   ./run_messenger.sh stop      # detiene uvicorn + cloudflared iniciados aquí
#
# Requisitos:
#   - .env con MESSENGER_VERIFY_TOKEN (lo que pegarás en el panel de Meta),
#     MESSENGER_PAGE_ID y MESSENGER_ACCESS_TOKEN (para poder enviar)
#   - cloudflared instalado (en PATH o como ./cloudflared)
#   - dependencias instaladas: pip install -r requirements.txt

set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8004}"
PID_PREFIX="messenger_"
LOGS_DIR="logs"

mkdir -p "$LOGS_DIR"

stop_tracked() {
  for name in uvicorn cloudflared; do
    local pidfile="$LOGS_DIR/${PID_PREFIX}$name.pid"
    if [[ -f "$pidfile" ]]; then
      local pid
      pid="$(cat "$pidfile")"
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        echo "Detenido proceso anterior de $name (pid $pid)"
      fi
      rm -f "$pidfile"
    fi
  done
  sleep 1
}

if [[ "${1:-}" == "stop" ]]; then
  stop_tracked
  echo "Detenido. Para volver a levantar: ./run_messenger.sh start"
  exit 0
fi

# --- Carga de .env -----------------------------------------------------------

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

[[ -n "${MESSENGER_VERIFY_TOKEN:-}" ]] || {
  echo "Falta MESSENGER_VERIFY_TOKEN en .env. Elige una cadena cualquiera y pégala también en el panel de Meta." >&2
  exit 1
}

# El verify token habilita el webhook, pero sin PAGE_ID o ACCESS_TOKEN el bot
# recibe mensajes y no puede responder: fallamos ya y no en silencio.
[[ -n "${MESSENGER_PAGE_ID:-}" ]] || {
  echo "Falta MESSENGER_PAGE_ID en .env (el id numérico de tu Página de Facebook)." >&2
  exit 1
}

[[ -n "${MESSENGER_ACCESS_TOKEN:-}" ]] || {
  echo "Falta MESSENGER_ACCESS_TOKEN en .env (token de la Página con permiso pages_messaging)." >&2
  exit 1
}

# --- Herramientas ------------------------------------------------------------

if [[ -x .venv/bin/uvicorn ]]; then
  UVICORN=".venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN="$(command -v uvicorn)"
else
  echo "No encuentro uvicorn. Activa el venv o: pip install -r requirements.txt" >&2
  exit 1
fi

if command -v cloudflared >/dev/null 2>&1; then
  CLOUDFLARED="$(command -v cloudflared)"
elif [[ -x ./cloudflared ]]; then
  CLOUDFLARED="./cloudflared"
else
  echo "No encuentro cloudflared. Instálalo:" >&2
  echo "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" >&2
  exit 1
fi

# --- Uvicorn -----------------------------------------------------------------

stop_tracked

if curl -sf "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"providers_configured"'; then
  echo "Uvicorn ya responde en el puerto $PORT (lo reutilizo)."
else
  echo "Levantando uvicorn en el puerto $PORT..."
  "$UVICORN" main:app --host 127.0.0.1 --port "$PORT" >"$LOGS_DIR/messenger_uvicorn.log" 2>&1 &
  echo $! > "$LOGS_DIR/${PID_PREFIX}uvicorn.pid"
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || {
    echo "Uvicorn no arrancó. Revisa $LOGS_DIR/messenger_uvicorn.log" >&2
    exit 1
  }
  echo "Uvicorn arriba (pid $(cat "$LOGS_DIR/${PID_PREFIX}uvicorn.pid"))."
fi

# --- Túnel de Cloudflare -----------------------------------------------------

echo "Abriendo túnel HTTPS (esto puede tardar unos segundos)..."
"$CLOUDFLARED" tunnel --url "http://127.0.0.1:$PORT" --protocol http2 >"$LOGS_DIR/messenger_cloudflared.log" 2>&1 &
echo $! > "$LOGS_DIR/${PID_PREFIX}cloudflared.pid"

url=""
for _ in $(seq 1 45); do
  url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS_DIR/messenger_cloudflared.log" 2>/dev/null | head -n1 || true)"
  [[ -n "$url" ]] && break
  sleep 1
done

[[ -n "$url" ]] || {
  echo "No obtuve la URL del túnel a tiempo. Revisa $LOGS_DIR/messenger_cloudflared.log" >&2
  exit 1
}

echo "Túnel listo: $url"
echo "$url" > "$LOGS_DIR/messenger_url.txt"

# --- Instrucciones para el panel de Meta -------------------------------------

echo
echo "LISTO. Para conectar Meta, en https://developers.facebook.com/apps/:"
echo
echo "  1. Tu app -> Messenger / Webhooks (según el tipo de app)"
echo "  2. Callback URL: $url/webhooks/messenger"
echo "  3. Verify token: $MESSENGER_VERIFY_TOKEN  (el que está en .env)"
echo "  4. Pulsa 'Verify and save'."
echo "  5. En 'Webhook fields', suscríbete a 'messages'."
echo "  6. Abre tu Página en Facebook -> botón 'Messenger' -> 'Enviar mensaje'."
echo
echo "Logs en: $LOGS_DIR/"
echo "Apagar: ./run_messenger.sh stop"
echo
echo "NOTA: la URL free de trycloudflare.com cambia en cada reinicio;"
echo "si reinicias el túnel, repite el paso 2 con la nueva URL."
echo "Ventana de 24h: solo se pueden mandar respuestas dentro de las 24h"
echo "tras el último mensaje del cliente."