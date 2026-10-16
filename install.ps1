# Installazione automatica su Windows.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1            installa tutto nella cartella
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Service   ...e avvio automatico all'accesso
#
# Non serve LM Studio: il modello linguistico gira con Ollama (portatile, dentro runtime\
# se Ollama non è già installato). Tutto resta in questa cartella. Non servono permessi di amministratore.
param([switch]$Service)
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot
$Root = $PSScriptRoot

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "`nERRORE: $msg" -ForegroundColor Red; exit 1 }

# I file temporanei vanno nel progetto: utile se il disco C: è quasi pieno
$Tmp = Join-Path $Root 'runtime\tmp'
New-Item -ItemType Directory -Force $Tmp | Out-Null
$env:TMP = $Tmp; $env:TEMP = $Tmp; $env:PIP_CACHE_DIR = Join-Path $Tmp 'pip'

# ------------------------------------------------------------------ Python
$Py = $null
foreach ($cand in @('py -3.12', 'py -3.11', 'py -3.10', 'python')) {
    $exe, $arg = $cand.Split(' ', 2)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $v = & $exe $arg -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
        if ($v -eq 'True') { $Py = $cand; break }
    }
}
if (-not $Py) { Die 'Serve Python 3.10 o più recente: https://www.python.org/downloads/ (spunta "Add to PATH")' }

Say "Ambiente Python (.venv) con $Py"
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    $exe, $arg = $Py.Split(' ', 2)
    & $exe $arg -m venv .venv
    if ($LASTEXITCODE) { Die 'Impossibile creare il venv' }
}
$VPy = Join-Path $Root '.venv\Scripts\python.exe'
& $VPy -m pip install -q --upgrade pip
& $VPy -m pip install -q -r requirements.txt
if ($LASTEXITCODE) { Die 'Installazione dei pacchetti Python fallita' }

$Gpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi -L *> $null
    if ($LASTEXITCODE -eq 0) { $Gpu = $true }
}
if ($Gpu) {
    Say 'GPU NVIDIA trovata: installo le librerie CUDA per Whisper'
    & $VPy -m pip install -q -r requirements-gpu.txt
} else {
    Say "Nessuna GPU NVIDIA: Whisper userà la CPU (modello 'small')"
}

# ------------------------------------------------------------------ Ollama
$Sys = Get-Command ollama -ErrorAction SilentlyContinue
$Portable = Join-Path $Root 'runtime\ollama\ollama.exe'
if ($Sys) {
    Say "Ollama già installato: $($Sys.Source)"
} elseif (Test-Path $Portable) {
    Say 'Ollama portatile già presente in runtime\ollama'
} else {
    Say 'Scarico Ollama portatile in runtime\ollama (circa 1,4 GB)'
    $zip = Join-Path $Tmp 'ollama.zip'
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -UseBasicParsing 'https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip' -OutFile $zip
    New-Item -ItemType Directory -Force 'runtime\ollama' | Out-Null
    # tar.exe di Windows (non quello di Git, che non capisce i percorsi tipo D:\)
    $WinTar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (Test-Path $WinTar) { & $WinTar -xf $zip -C 'runtime\ollama' }
    else { Expand-Archive -Path $zip -DestinationPath 'runtime\ollama' -Force }
    Remove-Item $zip
    if (-not (Test-Path $Portable)) { Die 'Estrazione di Ollama fallita' }
}

# ------------------------------------------------------------------ modelli
Say 'Scarico i modelli (voce, Whisper, LLM): solo questa volta'
if ($Gpu) { & $VPy scripts\download_models.py --gpu } else { & $VPy scripts\download_models.py }
if ($LASTEXITCODE) { Die 'Download dei modelli fallito' }

. (Join-Path $Root 'start.ps1') -OnlyOllama
$Model = & $VPy -c "from xiaozhi_locale.config import load; print(load()['llm']['model'])"
Say "Scarico il modello linguistico $Model"
& $OllamaBin pull $Model
if ($LASTEXITCODE) { Die "Download del modello $Model fallito" }
if ($OllamaProc) { Stop-Process -Id $OllamaProc.Id -Force -ErrorAction SilentlyContinue }

# ------------------------------------------------------------------ avvio automatico
if ($Service) {
    Say "Creo l'avvio automatico all'accesso (attività pianificata 'HuskyLocale')"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Root\start.ps1`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'HuskyLocale' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'HuskyLocale'
    Say 'Avvio automatico attivo. Per toglierlo:  Unregister-ScheduledTask HuskyLocale'
} else {
    Say 'Installazione completata. Avvia con doppio clic su avvia.bat'
    Write-Host "    Al primo avvio si apre la pagina di accesso: crea l'amministratore con il codice"
    Write-Host '    che il server stampa nella finestra del terminale.'
}
