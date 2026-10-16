"""Esegue tutti i test: avvia HuskyLens simulato + server di test, lancia le suite, spegne tutto.

Prerequisito: Ollama acceso col modello di config.yaml (start.ps1 -OnlyOllama / start.sh lo fa).
Uso:  .venv\\Scripts\\python tests\\run_all.py           (tutto, con il modello vero)
      .venv\\Scripts\\python tests\\run_all.py --mock    (senza Ollama: LLM finto, salta i test che
                                                       controllano le risposte del modello e la voce)
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def wait_health(url, need="", timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            body = urllib.request.urlopen(url, timeout=2).read().decode()
            if need in body:
                return True
        except OSError:
            pass
        time.sleep(2)
    return False


def stop(procs):
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def check_no_internet() -> int:
    """Dopo tutti i test: il server non deve avere connessioni verso Internet."""
    print("\n######## connessioni di rete del server")
    if sys.platform == "win32":
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(ROOT / "scripts" / "verifica_privacy.ps1")], capture_output=True, text=True).stdout
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


MOCK = "--mock" in sys.argv
MOCK_URL = "http://127.0.0.1:1299/v1"


def make_config(port: int, data_dir: str, auth: bool, turns: int, huskylens: bool, https: int = 0) -> str:
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    if MOCK:  # LLM finto dei test: nessun modello vero necessario
        cfg = cfg.replace("  base_url: http://127.0.0.1:11434/v1", f"  base_url: {MOCK_URL}")
    cfg = cfg.replace("port: 8000 ", f"port: {port} ").replace("https_port: 8443", f"https_port: {https}")
    cfg = cfg.replace("data_dir: data", f"data_dir: {data_dir}")  # dati dei test a parte: non tocca data/
    cfg = cfg.replace("turns_per_minute: 12", f"turns_per_minute: {turns}")
    if huskylens:
        cfg = cfg.replace("mcp_servers: []", "mcp_servers:\n  - name: huskylens\n    url: http://127.0.0.1:3000/sse")
    if not auth:
        cfg = cfg.replace("  tokens: []", '  tokens: ["test123"]')
        cfg = cfg.replace("auth:\n  # Accesso", "auth:\n  enabled: false\n  # Accesso", 1)
        cfg = cfg.replace("  enabled: true\n  require_2fa", "  require_2fa", 1)
    else:  # i test di sicurezza provano anche il primo avvio con il codice del terminale
        cfg = cfg.replace("  default_admin: true", "  default_admin: false")
    return cfg


def main():
    import shutil
    codes = []

    # ---------------- fase 0: componenti (senza server)
    print("\n######## tests/test_units.py")
    codes.append(subprocess.call([PY, "tests/test_units.py"], cwd=ROOT, env=env))

    mock = None
    if MOCK:
        print("\n######## modalità --mock: LLM finto sulla porta 1299 (salto i test sulle risposte del modello)")
        mock = subprocess.Popen([PY, "tests/mock_llm.py"], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return run_phases(codes, shutil)
    finally:
        if mock:
            stop([mock])


def run_phases(codes, shutil):
    # ---------------- fase 1: protocollo, voce, browser (account disattivati, token fisso)
    if MOCK:
        return run_phase2(codes, shutil)
    shutil.rmtree(ROOT / "tests" / "_data_test", ignore_errors=True)
    (ROOT / "tests" / "config.suite.yaml").write_text(
        make_config(8079, "tests/_data_test", auth=False, turns=1000, huskylens=True), encoding="utf-8")
    procs = [
        subprocess.Popen([PY, "tests/fake_huskylens.py"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([PY, "server.py", "-c", "tests/config.suite.yaml"], cwd=ROOT, env=env,
                         stdout=open(ROOT / "tests" / "_suite_server.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT),
    ]
    try:
        if not wait_health("http://127.0.0.1:8079/health", '"huskylens": 2'):
            print("Il server di test non è partito. Ultime righe del suo registro:")
            print("\n".join((ROOT / "tests" / "_suite_server.log").read_text(encoding="utf-8", errors="replace").splitlines()[-25:]))
            return 1
        for script in ("tests/test_suite.py", "tests/browser_mic_test.py"):
            print(f"\n######## {script}")
            codes.append(subprocess.call([PY, script], cwd=ROOT, env=env))
        codes.append(check_no_internet())
    finally:
        stop(procs)
    return run_phase2(codes, shutil)


def run_phase2(codes, shutil):
    # ---------------- fase 2: sicurezza (account attivi, primo avvio da zero)
    data_auth = ROOT / "tests" / "_data_auth"
    shutil.rmtree(data_auth, ignore_errors=True)
    data_auth.mkdir(parents=True)
    (ROOT / "tests" / "config.auth.yaml").write_text(
        make_config(8081, "tests/_data_auth", auth=True, turns=3, huskylens=False, https=8481), encoding="utf-8")
    log_path = data_auth / "server.log"
    with open(log_path, "w", encoding="utf-8") as logf:
        procs = [subprocess.Popen([PY, "server.py", "-c", "tests/config.auth.yaml"], cwd=ROOT, env=env,
                                  stdout=logf, stderr=subprocess.STDOUT)]
        try:
            if not wait_health("http://127.0.0.1:8081/health", '"ok"'):
                print("Il server di test (account) non è partito.")
                return 1
            print("\n######## tests/test_auth.py")
            codes.append(subprocess.call([PY, "tests/test_auth.py", "http://localhost:8081", str(log_path)],
                                         cwd=ROOT, env=env))
        finally:
            stop(procs)

    # ---------------- fase 3: MQTT (broker, sensori, ESPHome, LoRaWAN) e protocollo xiaozhi MQTT+UDP
    print("\n######## tests/test_mqtt.py")
    codes.append(subprocess.call([PY, "tests/test_mqtt.py"], cwd=ROOT, env=env))
    print("\n######## tests/test_xiaozhi_mqtt.py")
    codes.append(subprocess.call([PY, "tests/test_xiaozhi_mqtt.py", *(["--mock"] if MOCK else [])], cwd=ROOT, env=env))

    # ---------------- fase 4: aggiornamento con conservazione dei dati
    print("\n######## tests/test_update.py")
    codes.append(subprocess.call([PY, "tests/test_update.py"], cwd=ROOT, env=env))

    print("\nRISULTATO FINALE:", "TUTTO OK" if not any(codes) else "CI SONO ERRORI")
    return 1 if any(codes) else 0


if __name__ == "__main__":
    sys.exit(main())
