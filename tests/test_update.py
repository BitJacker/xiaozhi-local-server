"""Test dell'aggiornamento: una vecchia installazione con dati si aggiorna e i dati restano identici.

Crea in tests/_update_test una copia del programma "versione 1.0.0" con dati d'uso (utenti, cronologia,
dispositivi, impostazioni, modelli), finge una release su GitHub con un server locale e aggiorna.
Poi prova una release difettosa: l'aggiornamento deve fallire e rimettere la versione precedente.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale.updater import _managed_files  # noqa: E402

PY = sys.executable
T = ROOT / "tests" / "_update_test"
PORT = 8099
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


def program_files():
    for rel in _managed_files(ROOT):
        if rel.parts[0] == "tests" and len(rel.parts) > 1 and rel.parts[1].startswith(("_", "config.")):
            continue  # file generati dai test
        yield rel


def make_zip(version: str, extra: dict[str, str], drop_server=False, override: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in program_files():
            if drop_server and rel.as_posix() == "server.py":
                continue
            data = (ROOT / rel).read_bytes() if rel.as_posix() != "VERSION" else version.encode()
            if override and rel.as_posix() in override:
                data = override[rel.as_posix()](data)
            z.writestr(f"BitJacker-xiaozhi-local-server-abc123/{rel.as_posix()}", data)
        for name, content in extra.items():
            z.writestr(f"BitJacker-xiaozhi-local-server-abc123/{name}", content)
    return buf.getvalue()


RELEASES = {
    "/repos/test/repo/releases/latest": {"tag_name": "v1.2.0", "body": "- Novità di prova\n- Altra novità",
                                         "zipball_url": f"http://127.0.0.1:{PORT}/zip/good"},
    "/repos/test/bad/releases/latest": {"tag_name": "v9.0.0", "body": "release rotta",
                                        "zipball_url": f"http://127.0.0.1:{PORT}/zip/bad"},
    "/repos/test/crash/releases/latest": {"tag_name": "v9.1.0", "body": "si installa ma va in crash",
                                          "zipball_url": f"http://127.0.0.1:{PORT}/zip/crash"},
}
ZIPS = {}


class FakeGitHub(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in RELEASES:
            body, ctype = json.dumps(RELEASES[self.path]).encode(), "application/json"
        elif self.path in ZIPS:
            body, ctype = ZIPS[self.path], "application/zip"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def tree_hashes(base: Path) -> dict:
    # .manifest.json è l'appunto interno dell'aggiornamento (versione installata): cambia per scelta
    return {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in base.rglob("*") if p.is_file() and p.name != ".manifest.json"}


def run(*args):
    return subprocess.run([PY, "-m", "xiaozhi_locale.updater", *args], cwd=T, capture_output=True, text=True,
                          encoding="utf-8", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})


def main():
    # ---------------- installazione "vecchia" con un anno e mezzo di dati
    shutil.rmtree(T, ignore_errors=True)
    for rel in program_files():
        (T / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, T / rel)
    (T / "VERSION").write_text("1.0.0", encoding="utf-8")
    data = T / "data"
    (data / "history").mkdir(parents=True)
    (data / "config.yaml").write_text(
        "llm:\n  temperature: 0.3\nmcp_servers:\n  - name: huskylens\n    url: http://10.0.0.5:3000/sse\n"
        f"update:\n  api: http://127.0.0.1:{PORT}\n  repo: test/repo\n", encoding="utf-8")
    (data / "users.json").write_text(json.dumps({"admin": {"password": "scrypt$x$y", "role": "admin"}}), encoding="utf-8")
    (data / "devices.json").write_text(json.dumps({"aa:bb": {"status": "approved", "name": "Robot"}}), encoding="utf-8")
    (data / "secret.key").write_text("ab" * 32, encoding="utf-8")
    for i in range(30):
        (data / "history" / f"2025-{1 + i % 12:02d}-{1 + i:02d}.jsonl").write_text(
            json.dumps({"user": "admin", "role": "user", "text": f"domanda {i}"}) + "\n", encoding="utf-8")
    (T / "models" / "ollama").mkdir(parents=True)
    (T / "models" / "ollama" / "modello.bin").write_bytes(b"\x00" * 1000)
    (T / "runtime" / "ollama").mkdir(parents=True)
    (T / "runtime" / "ollama" / "ollama.exe").write_bytes(b"MZ" + b"\x00" * 100)
    # primo avvio: il programma registra com'era config.yaml; poi l'utente lo modifica a mano
    subprocess.run([PY, "-c", "from pathlib import Path; from xiaozhi_locale import updater; "
                              "updater.write_manifest(Path('data'))"], cwd=T, check=True)
    with open(T / "config.yaml", "a", encoding="utf-8") as f:
        f.write("\n# modifica fatta a mano dall'utente\n")
    data_before = tree_hashes(data)
    models_before = tree_hashes(T / "models")
    runtime_before = tree_hashes(T / "runtime")

    ZIPS["/zip/good"] = make_zip("1.2.0", {"NOVITA.txt": "file aggiunto nella 1.2.0"})
    ZIPS["/zip/bad"] = make_zip("9.0.0", {}, drop_server=True)
    ZIPS["/zip/crash"] = make_zip("9.1.0", {}, override={
        "xiaozhi_locale/app.py": lambda b: b"raise RuntimeError('versione rotta')\n" + b})
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        # ---------------- controllo
        r = run("--check")
        check("Controllo: trova la versione 1.2.0 con le novità",
              "ultima: 1.2.0" in r.stdout and "Novità di prova" in r.stdout, r.stdout.strip().splitlines()[0] if r.stdout else r.stderr[-200:])
        check("Controllo: non installa niente", (T / "VERSION").read_text().strip() == "1.0.0")

        # ---------------- aggiornamento
        r = run("--yes")
        check("Aggiornamento completato", r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1])
        check("Versione installata: 1.2.0", (T / "VERSION").read_text().strip() == "1.2.0")
        check("File nuovi della versione presenti", (T / "NOVITA.txt").exists())
        after = tree_hashes(data)
        same = {k: v for k, v in after.items() if k in data_before}
        check("Dati intatti: utenti, dispositivi, chiave, impostazioni, 30 giorni di cronologia",
              same == data_before, f"{len(data_before)} file controllati")
        check("Modelli e Ollama intatti", tree_hashes(T / "models") == models_before
              and {k: v for k, v in tree_hashes(T / "runtime").items() if k.startswith("ollama")} == runtime_before)
        kept = list(data.glob("config.modificato-*.yaml"))
        check("Modifiche a mano di config.yaml salvate in data/",
              len(kept) == 1 and "modifica fatta a mano" in kept[0].read_text(encoding="utf-8"))
        check("Backup della versione precedente creato", any((T / "runtime" / "backups").glob("codice-v1.0.0-*.zip")))
        pend = data / ".update_pending.json"
        check("Dopo l'aggiornamento: in attesa di conferma al riavvio", pend.exists())
        pend.unlink(missing_ok=True)  # al riavvio il server nuovo parte e conferma (confirm_update)
        out = subprocess.run([PY, "-c", "from xiaozhi_locale.config import load; c=load(); "
                                        "print(c['llm']['temperature'], c['mcp_servers'][0]['url'], c['_version'])"],
                             cwd=T, capture_output=True, text=True)
        check("Le impostazioni personali valgono anche nella nuova versione",
              out.stdout.split() == ["0.3", "http://10.0.0.5:3000/sse", "1.2.0"], out.stdout.strip() or out.stderr[-200:])

        # ---------------- aggiornamento difettoso: deve tornare indietro da solo
        cfg = (data / "config.yaml").read_text(encoding="utf-8").replace("repo: test/repo", "repo: test/bad")
        (data / "config.yaml").write_text(cfg, encoding="utf-8")
        before_bad = tree_hashes(data)
        r = run("--yes")
        check("Release difettosa: aggiornamento rifiutato", r.returncode != 0, (r.stdout.strip().splitlines() or [""])[-1])
        check("Release difettosa: resta la versione 1.2.0 funzionante",
              (T / "VERSION").read_text().strip() == "1.2.0" and (T / "server.py").exists())
        check("Release difettosa: dati intatti",
              {k: v for k, v in tree_hashes(data).items() if k in before_bad} == before_bad)

        # ---------------- release che si installa ma NON si avvia: la prova prima del riavvio la ferma
        cfg = (data / "config.yaml").read_text(encoding="utf-8").replace("repo: test/bad", "repo: test/crash")
        (data / "config.yaml").write_text(cfg, encoding="utf-8")
        r = run("--yes")
        out = r.stdout + r.stderr
        check("Release che va in crash: scoperta prima del riavvio", r.returncode != 0 and "non si avvia" in out,
              (out.strip().splitlines() or [""])[-1])
        check("Release che va in crash: rimessa la 1.2.0 funzionante",
              (T / "VERSION").read_text().strip() == "1.2.0"
              and "versione rotta" not in (T / "xiaozhi_locale" / "app.py").read_text(encoding="utf-8"))
        check("Release che va in crash: nessun riavvio in sospeso", not (data / ".update_pending.json").exists())

        # ---------------- nuova versione che non riesce mai a confermarsi dopo il riavvio
        backups = sorted((T / "runtime" / "backups").glob("codice-v1.0.0-*.zip"))
        (data / ".update_pending.json").write_text(json.dumps(
            {"backup": str(backups[0]), "from": "1.0.0", "to": "1.2.0", "attempts": 2}), encoding="utf-8")
        r = subprocess.run([PY, "server.py"], cwd=T, capture_output=True, text=True, timeout=300,
                           env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        check("Dopo 2 avvii falliti: al terzo torna da solo alla versione precedente",
              r.returncode == 42 and (T / "VERSION").read_text().strip() == "1.0.0", f"exit={r.returncode}")
        check("Ritorno indietro registrato (update_failed.json) e segnale rimosso",
              (data / "update_failed.json").exists() and not (data / ".update_pending.json").exists())
        check("Ritorno indietro: dati intatti",
              {k: v for k, v in tree_hashes(data).items() if k in before_bad and k != "config.yaml"}
              == {k: v for k, v in before_bad.items() if k != "config.yaml"})
    finally:
        srv.shutdown()

    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test di aggiornamento superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
