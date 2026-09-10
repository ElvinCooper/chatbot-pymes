#!/usr/bin/env bash
#
# Bot de Telegram en un comando.
#
# Levanta el chatbot (uvicorn) + un túnel HTTPS temporal de Cloudflare y
# registra el webhook en Telegram, para hablarle desde el celular sin
# dominios ni configuración manual.
#
# Uso:
#   ./run_bot.sh start      # levanta todo y deja el bot corriendo (predeterminado)
#   ./run_bot.sh stop       # detiene uvicorn + cloudflared iniciados aquí
#   ./run_bot.sh webhook    # solo re-registra el webhook con la última URL conocida
#
# Requisitos:
#   - .env con TELEGRAM_BOT_TOKEN (crea el bot con @BotFather)
#   - cloudflared instalado (en PATH o como ./cloudflared)
#   - dependencias instaladas: pip install -r requirements.txt

set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
LOGS_DIR="logs"
SECRET_VAR="TELEGRAM_WEBHOOK_SECRET"
URL_FILE="$LOGS_DIR/webhook_url.txt"

mkdir -p "$LOGS_DIR"

# --- Acciones ----------------------------------------------------------------

telegram_api() { # $1 = método; resto = args curl
  local method="$1"
  shift
  curl -sS -m 30 "$@" "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/$method"
}

stop_tracked() {
  for name in uvicorn cloudflared; do
    local pidfile="$LOGS_DIR/$name.pid"
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

register_webhook() { # $1 = url base del túnel
  local url="$1"
  local secret=""
  secret="${TELEGRAM_WEBHOOK_SECRET:-}"
  echo "Registrando webhook en Telegram..."
  local result
  result="$(telegram_api setWebhook \
    -F "url=$url/webhooks/telegram" \
    ${secret:+ -F "secret_token=$secret"})"
  echo "$result" > "$LOGS_DIR/webhook_set.json"
  echo "$url" > "$URL_FILE"
  echo "$result"
  echo
  echo "--- getWebhookInfo (debug) ---"
  telegram_api getWebhookInfo
}

# --- stop --------------------------------------------------------------------

if [[ "${1:-}" == "stop" ]]; then
  stop_tracked
  echo "Detenido. Para volver a levantar el bot: ./run_bot.sh start"
  exit 0
fi

# --- Carga de .env -----------------------------------------------------------

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

[[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] || {
  echo "Falta TELEGRAM_BOT_TOKEN en .env. Créalo con @BotFather y copia el token." >&2
  exit 1
}

# Si no hay secret de webhook, genera uno persistente y lo guarda en .env.
if [[ -z "${TELEGRAM_WEBHOOK_SECRET:-}" ]]; then
  local_secret="$(openssl rand -hex 16 2>/dev/null || od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  if grep -q "^$SECRET_VAR=" .env; then
    sed -i "s/^$SECRET_VAR=.*/$SECRET_VAR=$local_secret/" .env
  else
    echo "$SECRET_VAR=$local_secret" >> .env
  fi
  echo "Generado $SECRET_VAR persistente en .env (revísalo si quieres)."
  . ./.env
fi

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

# --- solo re-registrar webhook -----------------------------------------------

if [[ "${1:-}" == "webhook" ]]; then
  if [[ ! -f "$URL_FILE" ]]; then
    echo "No hay URL registrada (corre ./run_bot.sh start antes)." >&2
    exit 1
  fi
  register_webhook "$(cat "$URL_FILE")"
  exit 0
fi

# --- Uvicorn -----------------------------------------------------------------

stop_tracked

if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Uvicorn ya responde en el puerto $PORT (lo reutilizo)."
else
  echo "Levantando uvicorn en el puerto $PORT..."
  "$UVICORN" main:app --host 127.0.0.1 --port "$PORT" >"$LOGS_DIR/uvicorn.log" 2>&1 &
  echo $! > "$LOGS_DIR/uvicorn.pid"
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || {
    echo "Uvicorn no arrancó. Revisa $LOGS_DIR/uvicorn.log" >&2
    exit 1
  }
  echo "Uvicorn arriba (pid $(cat "$LOGS_DIR/uvicorn.pid"))."
fi

# --- Túnel de Cloudflare -----------------------------------------------------

echo "Abriendo túnel HTTPS (esto puede tardar unos segundos)..."
"$CLOUDFLARED" tunnel --url "http://127.0.0.1:$PORT" >"$LOGS_DIR/cloudflared.log" 2>&1 &
echo $! > "$LOGS_DIR/cloudflared.pid"

url=""
for _ in $(seq 1 45); do
  url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS_DIR/cloudflared.log" 2>/dev/null | head -n1 || true)"
  [[ -n "$url" ]] && break
  sleep 1
done

[[ -n "$url" ]] || {
  echo "No obtuve la URL del túnel a tiempo. Revisa $LOGS_DIR/cloudflared.log" >&2
  exit 1
}

echo "Túnel listo: $url"

# --- Webhook y cierre --------------------------------------------------------

register_webhook "$url"

echo
echo "LISTO. Desde tu celular abre el bot (el @username que te dio @BotFather), pulsa Start y escríbele."
echo "Logs en: $LOGS_DIR/"
echo "Apagar el bot: ./run_bot.sh stop"