#!/usr/bin/env bash
# Avvia Ollama (se non è già attivo) e il server. Usato anche dal servizio systemd.
#   ./start.sh                 avvia tutto
#   source ./start.sh --only-ollama   (uso interno di install.sh) avvia solo Ollama
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
ROOT="$(pwd)"

# Ollama ascolta SOLO su questo computer e tiene i modelli dentro il progetto
export OLLAMA_HOST="127.0.0.1:11434"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$ROOT/models/ollama}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:--1}"   # modello sempre in memoria: risposte rapide
# contesto da 8192 token (servono con molti strumenti HuskyLens) con cache compressa: poca memoria in più
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-8192}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"
mkdir -p "$OLLAMA_MODELS" "$ROOT/runtime"

if command -v ollama >/dev/null 2>&1; then OLLAMA_BIN="$(command -v ollama)"; else OLLAMA_BIN="$ROOT/runtime/ollama/bin/ollama"; fi
OLLAMA_STARTED_PID=""
if ! curl -fs "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  "$OLLAMA_BIN" serve >"$ROOT/runtime/ollama.log" 2>&1 &
  OLLAMA_STARTED_PID=$!
  for _ in $(seq 1 60); do
    curl -fs "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

if [ "${1:-}" = "--only-ollama" ]; then
  return 0 2>/dev/null || exit 0
fi

trap '[ -n "$OLLAMA_STARTED_PID" ] && kill "$OLLAMA_STARTED_PID" 2>/dev/null' EXIT
# se il server esce con codice 42 è appena stato aggiornato: si riavvia da solo
while true; do
  code=0
  .venv/bin/python -u server.py "$@" || code=$?
  [ "$code" -eq 42 ] || exit "$code"
  echo "==> Aggiornamento installato: riavvio del server..."
done
