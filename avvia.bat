@echo off
REM Doppio clic: avvia Ollama e il server, poi apre il client web.
cd /d "%~dp0"
start "" /b cmd /c "timeout /t 15 >nul & start http://localhost:8000/"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
pause
