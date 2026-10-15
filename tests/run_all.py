"""Esegue tutti i test: avvia HuskyLens simulato + server di test, lancia le suite, spegne tutto.

Prerequisito: LM Studio acceso col modello di config.yaml (avvia.bat lo fa).
Uso:  .venv\\Scripts\\python tests\\run_all.py
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def wait_health(url, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            body = urllib.request.urlopen(url, timeout=2).read().decode()
            if '"huskylens": 2' in body:
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def main():
    # config di test: porta 8079, token, HuskyLens simulato
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("port: 8000", "port: 8079").replace("  tokens: []", '  tokens: ["test123"]')
    cfg = cfg.replace("mcp_servers: []", "mcp_servers:\n  - name: huskylens\n    url: http://127.0.0.1:3000/sse")
    (ROOT / "tests" / "config.suite.yaml").write_text(cfg, encoding="utf-8")

    procs = [
        subprocess.Popen([PY, "tests/fake_huskylens.py"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([PY, "server.py", "-c", "tests/config.suite.yaml"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        if not wait_health("http://localhost:8079/health"):
            print("Il server di test non è partito.")
            return 1
        codes = []
        for script in ("tests/test_suite.py", "tests/browser_mic_test.py"):
            print(f"\n######## {script}")
            codes.append(subprocess.call([PY, script], cwd=ROOT, env=env))
        print("\nRISULTATO FINALE:", "TUTTO OK" if not any(codes) else "CI SONO ERRORI")
        return 1 if any(codes) else 0
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    sys.exit(main())
