#!/usr/bin/env bash
# Installazione automatica su Linux (Ubuntu/Debian e simili).
#
#   ./install.sh             installa tutto nella cartella del progetto
#   ./install.sh --service   ...e crea il servizio systemd che parte all'avvio del server
#
# Non serve LM Studio: il modello linguistico gira con Ollama (portatile, dentro runtime/
# se Ollama non è già installato nel sistema). Tutto resta in questa cartella.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
ROOT="$(pwd)"
SERVICE=0
LIGHT="${HUSKY_PROFILE:-auto}"
for a in "$@"; do
  [ "$a" = "--service" ] && SERVICE=1
  [ "$a" = "--light" ] && LIGHT=yes
  [ "$a" = "--full" ] && LIGHT=no
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERRORE: %s\033[0m\n' "$*"; exit 1; }

# ------------------------------------------------------------------ pacchetti di sistema
# Su Debian/Ubuntu installa da solo ciò che manca (serve root o sudo).
need=()
command -v python3 >/dev/null || need+=(python3)
python3 -c 'import ensurepip, venv' 2>/dev/null || need+=(python3-venv)
command -v curl >/dev/null || need+=(curl)
command -v zstd >/dev/null || need+=(zstd)
if [ ${#need[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    say "Installo i pacchetti di sistema: ${need[*]}"
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "${need[@]}" \
      || die "Installa a mano: sudo apt install ${need[*]}"
  else
    die "Mancano: ${need[*]}. Installali con il gestore pacchetti della tua distribuzione."
  fi
fi

# ------------------------------------------------------------------ Python
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Serve Python 3.10 o più recente"

say "Ambiente Python (.venv)"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || die "Impossibile creare il venv. Su Ubuntu: sudo apt install python3-venv"
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

GPU=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU=1
  say "GPU NVIDIA trovata: installo le librerie CUDA per Whisper"
  .venv/bin/python -m pip install -q -r requirements-gpu.txt
else
  say "Nessuna GPU NVIDIA: Whisper userà la CPU"
fi

# ------------------------------------------------------------------ profilo leggero (Raspberry Pi / poca RAM)
RAM_GB=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 8)
MODEL_HW="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
if [ "$LIGHT" = auto ]; then
  LIGHT=no
  if [[ "$MODEL_HW" == *"Raspberry Pi"* ]] || { [ "$GPU" = 0 ] && [ "$RAM_GB" -lt 12 ]; }; then LIGHT=yes; fi
fi
mkdir -p data
if [ "$LIGHT" = yes ] && ! grep -qv '^\s*#\|^\s*$' data/config.yaml 2>/dev/null; then
  if [ "$RAM_GB" -ge 7 ]; then LLM_SMALL="qwen2.5:3b-instruct-q4_K_M"; else LLM_SMALL="qwen2.5:1.5b-instruct-q4_K_M"; fi
  say "Profilo leggero (${MODEL_HW:-senza GPU}, ${RAM_GB} GB di RAM): Whisper base, modello $LLM_SMALL"
  cat > data/config.yaml <<EOF
# Profilo leggero scelto da install.sh per ${MODEL_HW:-un computer senza GPU} con ${RAM_GB} GB di RAM.
# Puoi cambiarlo: queste righe sostituiscono i valori di config.yaml.
asr:
  model: base
  cpu_model: base
  beam_size: 1
llm:
  model: $LLM_SMALL
  max_tokens: 250
limits:
  turns_per_minute: 6
EOF
fi

# ------------------------------------------------------------------ Ollama
if command -v ollama >/dev/null 2>&1; then
  say "Ollama già installato nel sistema: $(command -v ollama)"
elif [ -x runtime/ollama/bin/ollama ]; then
  say "Ollama portatile già presente in runtime/ollama"
else
  say "Scarico Ollama portatile in runtime/ollama (circa 1,4 GB)"
  case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) die "Architettura $(uname -m) non supportata da Ollama" ;;
  esac
  mkdir -p runtime/ollama
  curl -fL --progress-bar "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-${ARCH}.tar.zst" \
    | zstd -d | tar -x -C runtime/ollama
fi

# ------------------------------------------------------------------ modelli
say "Scarico i modelli (voce, Whisper, LLM): solo questa volta"
if [ "$GPU" = 1 ]; then .venv/bin/python scripts/download_models.py --gpu; else .venv/bin/python scripts/download_models.py; fi

# avvia Ollama solo il tempo di scaricare il modello linguistico
source ./start.sh --only-ollama
MODEL="$(.venv/bin/python -c 'from xiaozhi_locale.config import load; print(load()["llm"]["model"])')"
say "Scarico il modello linguistico $MODEL"
"$OLLAMA_BIN" pull "$MODEL"
if [ -n "${OLLAMA_STARTED_PID:-}" ]; then kill "$OLLAMA_STARTED_PID" 2>/dev/null || true; fi

# ------------------------------------------------------------------ servizio
if [ "$SERVICE" = 1 ]; then
  command -v systemctl >/dev/null || die "systemd non disponibile: avvia a mano con ./start.sh"
  say "Creo il servizio systemd 'husky-locale'"
  if [ "$(id -u)" -eq 0 ]; then
    echo "    ATTENZIONE: stai installando come root, quindi anche il server girerà come root."
    echo "    È più sicuro installarlo con un utente normale (il servizio userà quell'utente)."
  fi
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO tee /etc/systemd/system/husky-locale.service >/dev/null <<EOF
[Unit]
Description=Husky locale - server xiaozhi privacy-first
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$ROOT
ExecStart=$ROOT/start.sh
Restart=always
RestartSec=5
# protezioni: il server non può ottenere privilegi, né modificare il sistema (/usr, /etc, /boot) o il kernel;
# i file che crea (dati, cronologia) sono leggibili solo dal suo utente
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now husky-locale
  say "Servizio attivo. Log:  journalctl -u husky-locale -f"
  echo "    Il codice per creare l'amministratore è nel log:  journalctl -u husky-locale | grep -A1 PRIMO"
else
  say "Installazione completata. Avvia con:  ./start.sh"
  echo "    Al primo avvio apri http://IP-DEL-SERVER:8000/login e crea l'amministratore"
  echo "    con il codice che il server stampa nel terminale."
fi
