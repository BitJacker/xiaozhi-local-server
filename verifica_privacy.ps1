# Mostra tutte le connessioni di rete del server Husky e di LM Studio
# e segnala quelle verso Internet (fuori da questo PC e dalla rete locale).
# Uso:  powershell -ExecutionPolicy Bypass -File verifica_privacy.ps1

function Is-Local($ip) {
    return ($ip -match '^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.0\.0\.0|::1?$|::$|fe80:)')
}

$procs = @()
$procs += Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'server\.py' }
$procs += Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'lm ?studio|llmster|lms' }

if (-not $procs) { Write-Host "Server non in esecuzione."; exit 1 }

$esterne = 0
foreach ($p in $procs) {
    $conns = Get-NetTCPConnection -OwningProcess $p.ProcessId -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        if ($c.State -eq 'Listen') { continue }
        $locale = Is-Local $c.RemoteAddress
        if (-not $locale) { $esterne++ }
        $tag = 'LOCALE  '
        if (-not $locale) { $tag = 'INTERNET' }
        Write-Host ("[{0}] {1,-14} {2}:{3} -> {4}:{5} ({6})" -f $tag, $p.Name, $c.LocalAddress, $c.LocalPort, $c.RemoteAddress, $c.RemotePort, $c.State)
    }
}
Write-Host ""
if ($esterne -eq 0) {
    Write-Host "OK: nessuna connessione verso Internet. I dati restano su questo PC / rete locale." -ForegroundColor Green
} else {
    Write-Host "ATTENZIONE: $esterne connessioni verso Internet (vedi righe INTERNET)." -ForegroundColor Red
}
