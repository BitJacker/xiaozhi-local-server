#!/bin/sh
# Avvio nel container: la prima volta scarica voce e modello Whisper nel volume /app/models, poi avvia il server.
set -e
cd /app
if [ "${HUSKY_GPU:-0}" = "1" ]; then
    python scripts/download_models.py --gpu
else
    python scripts/download_models.py
fi
exec python server.py "$@"
