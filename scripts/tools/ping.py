"""Strumento predefinito: controlla se un dispositivo della rete risponde.

Uso: python scripts/tools/ping.py 192.168.1.50
L'indirizzo viene controllato di nuovo qui (solo lettere, numeri, punti e trattini) e passato a ping
come argomento separato, senza shell.
"""
import re
import subprocess
import sys

host = sys.argv[1] if len(sys.argv) > 1 else ""
if not re.fullmatch(r"[A-Za-z0-9.\-]{1,64}", host) or host.startswith("-"):
    print("Indirizzo non valido.")
    sys.exit(1)
count = ["-n", "1", "-w", "1500"] if sys.platform == "win32" else ["-c", "1", "-W", "2"]
try:
    ok = subprocess.run(["ping", *count, host], capture_output=True, timeout=6).returncode == 0
except (OSError, subprocess.TimeoutExpired):
    ok = False
print(f"{host} risponde: è acceso e collegato alla rete." if ok else f"{host} non risponde.")
