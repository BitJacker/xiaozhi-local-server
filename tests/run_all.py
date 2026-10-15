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


def check_no_internet() -> int:
    """Dopo tutti i test: il server non deve avere connessioni verso Internet."""
    print("\n######## connessioni di rete del server")
    if sys.platform == "win32":
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(ROOT / "verifica_privacy.ps1")], capture_output=True, text=True).stdout
        print(out.strip())
        ok = "OK: nessuna connessione verso Internet" in out
    else:
        out = subprocess.run(["ss", "-tnpH"], capture_output=True, text=True).stdout
        bad = [l for l in out.splitlines() if ("python" in l or "ollama" in l)
               and not any(x in l.split()[4] for x in ("127.0.0.1", "[::1]", "192.168.", "10.", "172."))]
        print("\n".join(bad) or "nessuna connessione esterna")
        ok = not bad
    print(f"[{'OK ' if ok else 'KO '}] Nessuna connessione verso Internet durante i test")
    return 0 if ok else 1


def main():
    # config di test: porta 8079, token, HuskyLens simulato
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("port: 8000", "port: 8079").replace("  tokens: []", '  tokens: ["test123"]')
    cfg = cfg.replace("mcp_servers: []", "mcp_servers:\n  - name: huskylens\n    url: http://127.0.0.1:3000/sse")
    # cronologia dei test in una cartella a parte (non tocca quella vera)
    cfg = cfg.replace("dir: data/history", "dir: tests/_history_test")
    import shutil
    shutil.rmtree(ROOT / "tests" / "_history_test", ignore_errors=True)
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
        codes.append(check_no_internet())
        print("\nRISULTATO FINALE:", "TUTTO OK" if not any(codes) else "CI SONO ERRORI")
        return 1 if any(codes) else 0
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    sys.exit(main())
