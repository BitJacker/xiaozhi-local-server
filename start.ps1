# Avvia Ollama (se non è già attivo) e il server. Usato anche dall'avvio automatico.
#   powershell -ExecutionPolicy Bypass -File start.ps1
#   . .\start.ps1 -OnlyOllama     (uso interno di install.ps1) avvia solo Ollama
param([switch]$OnlyOllama)
$Root = $PSScriptRoot
Set-Location -Path $Root

# Ollama ascolta SOLO su questo computer e tiene i modelli dentro il progetto
$env:OLLAMA_HOST = '127.0.0.1:11434'
if (-not $env:OLLAMA_MODELS) { $env:OLLAMA_MODELS = Join-Path $Root 'models\ollama' }
if (-not $env:OLLAMA_KEEP_ALIVE) { $env:OLLAMA_KEEP_ALIVE = '-1' }   # modello sempre in memoria
# contesto da 8192 token (servono con molti strumenti HuskyLens) con cache compressa: poca memoria in più
if (-not $env:OLLAMA_CONTEXT_LENGTH) { $env:OLLAMA_CONTEXT_LENGTH = '8192' }
if (-not $env:OLLAMA_FLASH_ATTENTION) { $env:OLLAMA_FLASH_ATTENTION = '1' }
if (-not $env:OLLAMA_KV_CACHE_TYPE) { $env:OLLAMA_KV_CACHE_TYPE = 'q8_0' }
New-Item -ItemType Directory -Force $env:OLLAMA_MODELS, (Join-Path $Root 'runtime') | Out-Null

$Sys = Get-Command ollama -ErrorAction SilentlyContinue
if ($Sys) { $OllamaBin = $Sys.Source } else { $OllamaBin = Join-Path $Root 'runtime\ollama\ollama.exe' }

function Test-Ollama {
    try { $null = Invoke-WebRequest -UseBasicParsing "http://$($env:OLLAMA_HOST)/api/version" -TimeoutSec 2; return $true }
    catch { return $false }
}

$OllamaProc = $null
if (-not (Test-Ollama)) {
    $OllamaProc = Start-Process -FilePath $OllamaBin -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Root 'runtime\ollama.out.log') `
        -RedirectStandardError (Join-Path $Root 'runtime\ollama.log')
    for ($i = 0; $i -lt 60; $i++) { if (Test-Ollama) { break }; Start-Sleep -Milliseconds 500 }
}

if ($OnlyOllama) { return }

try {
    # se il server esce con codice 42 è appena stato aggiornato: si riavvia da solo
    do {
        & (Join-Path $Root '.venv\Scripts\python.exe') -u server.py @args
        $code = $LASTEXITCODE
        # 42 = appena aggiornato; un crash subito dopo un aggiornamento: si riprova (dopo 2 tentativi torna indietro)
        $retry = ($code -eq 42) -or (($code -ne 0) -and (Test-Path (Join-Path $Root 'data\.update_pending.json')))
        if ($retry) { Write-Host '==> Riavvio del server...'; Start-Sleep -Seconds 3 }
    } while ($retry)
} finally {
    if ($OllamaProc) { Stop-Process -Id $OllamaProc.Id -Force -ErrorAction SilentlyContinue }
}
