#!/usr/bin/env bash
# Aggiorna Husky locale all'ultima versione mantenendo tutti i dati (cartella data/).
#   ./update.sh            mostra le novità e chiede conferma
#   ./update.sh --check    controlla soltanto
#   ./update.sh --yes      aggiorna senza chiedere
# Poi riavvia il server (./start.sh) o il servizio: sudo systemctl restart husky-locale
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python -m xiaozhi_locale.updater "$@"
