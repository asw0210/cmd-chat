#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${CMD_CHAT_HOST:-127.0.0.1}"
PORT="${CMD_CHAT_PORT:-3000}"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"
PASSWORD="${CMD_CHAT_PASSWORD:-}"
PUBLIC_HOSTNAME="${CMD_CHAT_PUBLIC_HOSTNAME:-${1:-}}"
TUNNEL_ID="${CMD_CHAT_TUNNEL_ID:-}"
TUNNEL_CREDENTIALS_FILE="${CMD_CHAT_TUNNEL_CREDENTIALS_FILE:-}"
CLOUDFLARED_CONFIG="${CMD_CHAT_CLOUDFLARED_CONFIG:-$HOME/.cloudflared/config.yml}"

SERVER_LOG="$(mktemp "${TMPDIR:-/tmp}/cmd-chat-server.XXXXXX.log")"
TUNNEL_LOG="$(mktemp "${TMPDIR:-/tmp}/cmd-chat-cloudflared.XXXXXX.log")"
TUNNEL_CONFIG=""
SERVER_PID=""
TUNNEL_PID=""

cleanup() {
    set +e
    if [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        kill "$TUNNEL_PID" 2>/dev/null
    fi
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
    fi
    wait "$TUNNEL_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    rm -f "$SERVER_LOG" "$TUNNEL_LOG" "$TUNNEL_CONFIG"
}
trap cleanup EXIT INT TERM

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

if [[ ! -x "$PYTHON" ]]; then
    echo "Python executable not found or not executable: $PYTHON" >&2
    echo "Set PYTHON=/path/to/python or create the repo .venv first." >&2
    exit 1
fi

need_cmd "$CLOUDFLARED"

if [[ -z "$PASSWORD" ]]; then
    read -r -s -p "Room password: " PASSWORD
    echo
fi

if [[ -n "$PUBLIC_HOSTNAME" ]]; then
    if [[ -z "$TUNNEL_ID" && -f "$CLOUDFLARED_CONFIG" ]]; then
        TUNNEL_ID="$(awk '/^tunnel:/ {print $2; exit}' "$CLOUDFLARED_CONFIG")"
    fi
    if [[ -z "$TUNNEL_CREDENTIALS_FILE" && -f "$CLOUDFLARED_CONFIG" ]]; then
        TUNNEL_CREDENTIALS_FILE="$(awk '/^credentials-file:/ {print $2; exit}' "$CLOUDFLARED_CONFIG")"
    fi

    if [[ -z "$TUNNEL_ID" || -z "$TUNNEL_CREDENTIALS_FILE" ]]; then
        echo "Custom hostnames require a named Cloudflare tunnel." >&2
        echo "Set CMD_CHAT_TUNNEL_ID and CMD_CHAT_TUNNEL_CREDENTIALS_FILE, or configure $CLOUDFLARED_CONFIG." >&2
        exit 1
    fi

    if [[ ! -f "$TUNNEL_CREDENTIALS_FILE" ]]; then
        echo "Cloudflare tunnel credentials not found: $TUNNEL_CREDENTIALS_FILE" >&2
        exit 1
    fi
fi

echo "Starting cmd-chat on http://$HOST:$PORT ..."
"$PYTHON" "$ROOT_DIR/cmd_chat.py" serve "$HOST" "$PORT" --password "$PASSWORD" >"$SERVER_LOG" 2>&1 &
SERVER_PID="$!"

for _ in {1..60}; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "cmd-chat server failed to start:" >&2
        sed -n '1,120p' "$SERVER_LOG" >&2
        exit 1
    fi

    if "$PYTHON" - "$HOST" "$PORT" >/dev/null 2>&1 <<'PY'
import http.client
import sys

host, port = sys.argv[1], int(sys.argv[2])
conn = http.client.HTTPConnection(host, port, timeout=1)
conn.request("GET", "/health")
resp = conn.getresponse()
raise SystemExit(0 if resp.status == 200 else 1)
PY
    then
        break
    fi

    sleep 0.25
done

if ! "$PYTHON" - "$HOST" "$PORT" >/dev/null 2>&1 <<'PY'
import http.client
import sys

host, port = sys.argv[1], int(sys.argv[2])
conn = http.client.HTTPConnection(host, port, timeout=1)
conn.request("GET", "/health")
resp = conn.getresponse()
raise SystemExit(0 if resp.status == 200 else 1)
PY
then
    echo "cmd-chat server did not become healthy at http://$HOST:$PORT/health" >&2
    sed -n '1,120p' "$SERVER_LOG" >&2
    exit 1
fi

if [[ -n "$PUBLIC_HOSTNAME" ]]; then
    echo "Starting cloudflared named tunnel for https://$PUBLIC_HOSTNAME ..."
    TUNNEL_CONFIG="$(mktemp "${TMPDIR:-/tmp}/cmd-chat-cloudflared.XXXXXX.yml")"
    cat >"$TUNNEL_CONFIG" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $TUNNEL_CREDENTIALS_FILE

ingress:
  - hostname: $PUBLIC_HOSTNAME
    service: http://$HOST:$PORT
  - service: http_status:404
EOF
    "$CLOUDFLARED" tunnel --config "$TUNNEL_CONFIG" run >"$TUNNEL_LOG" 2>&1 &
else
    echo "Starting cloudflared quick tunnel ..."
    "$CLOUDFLARED" tunnel --url "http://$HOST:$PORT" >"$TUNNEL_LOG" 2>&1 &
fi
TUNNEL_PID="$!"

PUBLIC_URL="${PUBLIC_HOSTNAME:+https://$PUBLIC_HOSTNAME}"
for _ in {1..120}; do
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        echo "cloudflared failed to start:" >&2
        sed -n '1,160p' "$TUNNEL_LOG" >&2
        exit 1
    fi

    if [[ -n "$PUBLIC_URL" ]]; then
        break
    fi

    PUBLIC_URL="$(grep -Eo 'https://[-a-zA-Z0-9.]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)"

    sleep 0.5
done

if [[ -z "$PUBLIC_URL" ]]; then
    echo "Timed out waiting for cloudflared to publish a URL." >&2
    sed -n '1,160p' "$TUNNEL_LOG" >&2
    exit 1
fi

cat <<EOF

cmd-chat is hosted:
  $PUBLIC_URL

Connect with:
  $PYTHON "$ROOT_DIR/cmd_chat.py" connect $PUBLIC_URL 443 USERNAME '<password>'

Press Ctrl+C to stop the server and tunnel.
EOF

wait "$SERVER_PID" "$TUNNEL_PID"
