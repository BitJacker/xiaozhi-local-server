"""Aggiornamento REALE da una versione pubblicata (tag Git, es. v1.1.0) alla versione attuale.

Usa il codice di aggiornamento della versione VECCHIA, come farebbe chi preme "Aggiorna" sul suo server:
1. estrae il tag (git archive) in tests/_upgrade_test e ci crea dati con il codice vecchio
   (admin con 2FA, dispositivo approvato, cronologia);
2. finge una release su GitHub con il codice attuale e lancia l'aggiornatore vecchio;
3. con il codice nuovo verifica che login + 2FA, dispositivo e cronologia funzionino ancora.
Uso:  python tests/test_upgrade_real.py [v1.1.0]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import test_update as tu  # noqa: E402

TAG = sys.argv[1] if len(sys.argv) > 1 else "v1.1.0"
T = ROOT / "tests" / "_upgrade_test"
PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


def py(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-c", code], cwd=T, capture_output=True, text=True, env=ENV, timeout=300)


def main():
    shutil.rmtree(T, ignore_errors=True)
    T.mkdir(parents=True)
    arch = subprocess.run(["git", "archive", "--format=tar", TAG], cwd=ROOT, capture_output=True)
    if arch.returncode:
        print("Tag non trovato:", TAG)
        return 1
    subprocess.run(["tar", "-x", "-C", str(T)], input=arch.stdout, check=True)
    old_version = (T / "VERSION").read_text().strip()
    print(f"Installazione vecchia: {TAG} (VERSION {old_version})")

    data = T / "data"
    data.mkdir()
    (data / "config.yaml").write_text(f"update:\n  api: http://127.0.0.1:{tu.PORT}\n  repo: test/repo\n", encoding="utf-8")
    # ---------------- dati creati dal codice VECCHIO
    r = py('''
import json, time
from pathlib import Path
from xiaozhi_locale.config import load
from xiaozhi_locale.auth import Auth, totp
from xiaozhi_locale.history import History
cfg = load(); d = Path(cfg["_data_dir"])
a = Auth(cfg["auth"], d)
assert a.create_user("admin", "Password-Admin-1", "admin") is None
s = a.totp_begin("admin"); assert a.totp_confirm("admin", totp(s, int(time.time() // 30)))
try:
    st = a.device_status("AA:BB:CC:00:11:22", "client-vero")  # dalla 1.2.0: MAC + Client-Id
except TypeError:
    st = a.device_status("AA:BB:CC:00:11:22")  # 1.1.0: solo MAC
assert a.approve_device(code=st["code"], name="Robot") is None
h = History(cfg["history"], d); h.append("admin", "web", "s1", "user", "domanda della versione vecchia")
print(json.dumps({"secret": s}))
''')
    if r.returncode:
        print(r.stdout, r.stderr[-1500:])
        return 1
    secret = json.loads(r.stdout.strip().splitlines()[-1])["secret"]
    check("Dati creati con la versione vecchia (admin con 2FA, dispositivo, cronologia)", True)
    snapshot = tu.tree_hashes(data)

    # ---------------- release finta = codice attuale
    tu.ZIPS["/zip/good"] = tu.make_zip((ROOT / "VERSION").read_text().strip(), {})
    tu.RELEASES["/repos/test/repo/releases/latest"]["tag_name"] = "v" + (ROOT / "VERSION").read_text().strip()
    srv = ThreadingHTTPServer(("127.0.0.1", tu.PORT), tu.FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run([PY, "-m", "xiaozhi_locale.updater", "--yes"], cwd=T, capture_output=True, text=True,
                           env=ENV, timeout=900)
    finally:
        srv.shutdown()
    new_version = (T / "VERSION").read_text().strip()
    check(f"Aggiornatore della {old_version} installa la nuova versione", r.returncode == 0 and
          new_version == (ROOT / "VERSION").read_text().strip(), f"{old_version} -> {new_version}")
    after = tu.tree_hashes(data)
    check("Dati della versione vecchia intatti dopo l'aggiornamento",
          {k: v for k, v in after.items() if k in snapshot} == snapshot, f"{len(snapshot)} file")

    # ---------------- il codice NUOVO lavora con i dati vecchi
    r = py(f'''
import json, time
from pathlib import Path
from xiaozhi_locale.config import load
from xiaozhi_locale.auth import Auth, totp
from xiaozhi_locale.history import History
from xiaozhi_locale.cryptobox import from_config
cfg = load(); d = Path(cfg["_data_dir"]); box = from_config(cfg, d)
a = Auth(cfg["auth"], d); a.box = box
tok, err = a.login("admin", "Password-Admin-1", totp("{secret}", int(time.time() // 30) + 1), "127.0.0.1")
out = {{"login_2fa": bool(tok), "err": err}}
st = a.device_status("AA:BB:CC:00:11:22", "client-vero")
out["device_migrated"] = st["status"] == "approved"
out["device_ok"] = bool(a.device_ok("AA:BB:CC:00:11:22", "client-vero", a.device_token("AA:BB:CC:00:11:22", "client-vero")))
st2 = a.device_status("AA:BB:CC:00:11:22", "client-ladro")
out["impostor_pending"] = st2["status"] == "pending"
h = History(cfg["history"], d, box)
day = h.days()[0]
h.append("admin", "web", "s2", "user", "domanda della versione nuova")
msgs = [m["text"] for m in h.read(day)]
out["history_old"] = "domanda della versione vecchia" in msgs
out["history_new_encrypted"] = "versione nuova" not in (d / "history" / f"{{day}}.jsonl").read_text(encoding="utf-8")
out["history_new_readable"] = "domanda della versione nuova" in msgs or "domanda della versione nuova" in [m["text"] for m in h.read(time.strftime("%Y-%m-%d"))]
print(json.dumps(out))
''')
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        print(r.stdout, r.stderr[-1500:])
        return 1
    check("Login con password e 2FA della versione vecchia", out["login_2fa"], str(out.get("err") or ""))
    check("Dispositivo approvato nella versione vecchia: ancora approvato (legato al suo Client-Id)",
          out["device_migrated"] and out["device_ok"])
    check("Dopo la migrazione un altro Client-Id con lo stesso MAC deve essere approvato di nuovo", out["impostor_pending"])
    check("Cronologia della versione vecchia leggibile", out["history_old"])
    check("Cronologia nuova cifrata e leggibile", out["history_new_encrypted"] and out["history_new_readable"])
    sel = subprocess.run([PY, "server.py", "--selftest"], cwd=T, capture_output=True, text=True, env=ENV, timeout=300)
    check("La nuova versione si avvia con i dati vecchi", "SELFTEST OK" in sel.stdout, sel.stdout.strip()[-60:])

    shutil.rmtree(T, ignore_errors=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test di aggiornamento reale ({TAG} -> attuale) superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
