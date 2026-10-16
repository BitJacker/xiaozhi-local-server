# Aggiorna Husky locale all'ultima versione mantenendo tutti i dati (cartella data\).
#   powershell -ExecutionPolicy Bypass -File update.ps1           mostra le novità e chiede conferma
#   powershell -ExecutionPolicy Bypass -File update.ps1 --check   controlla soltanto
#   powershell -ExecutionPolicy Bypass -File update.ps1 --yes     aggiorna senza chiedere
# Poi riavvia il server (avvia.bat).
Set-Location -Path $PSScriptRoot
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -m xiaozhi_locale.updater @args
exit $LASTEXITCODE
