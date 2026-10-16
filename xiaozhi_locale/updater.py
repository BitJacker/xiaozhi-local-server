"""Aggiornamenti: controlla le release su GitHub e installa la nuova versione CONSERVANDO I DATI.

Cosa viene sostituito: solo il codice del programma.
Cosa NON viene mai toccato: data/ (impostazioni, utenti, dispositivi, cronologia), models/, runtime/, .venv/.

Niente viene mai installato in automatico: l'admin vede le novità e preme "Aggiorna".
Privacy: GitHub viene contattato solo dalla pagina Admin (all'apertura, se update.check_on_admin_open è attivo,
o col pulsante "Controlla") e da update.sh / update.ps1. Non viene inviato nessun dato dell'utente.

Uso da terminale:  python -m xiaozhi_locale.updater [--check] [--yes]
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from .config import ROOT, version

log = logging.getLogger("updater")
PROTECTED = {"data", "models", "runtime", ".venv", ".git", "__pycache__"}
RESTART_CODE = 42  # start.sh / start.ps1 riavviano il server quando esce con questo codice
DEFAULT_REPO = "BitJacker/xiaozhi-local-server"


def parse_version(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV")
    out = []
    for part in v.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _http_json(url: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "husky-locale-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def check(cfg: dict) -> dict:
    """Chiede a GitHub l'ultima release. Ritorna versione attuale, ultima, novità e se serve aggiornare."""
    ucfg = cfg.get("update") or {}
    api = (ucfg.get("api") or "https://api.github.com").rstrip("/")
    repo = ucfg.get("repo") or DEFAULT_REPO
    rel = _http_json(f"{api}/repos/{repo}/releases/latest")
    latest = rel.get("tag_name", "")
    current = version()
    return {
        "current": current,
        "latest": latest.lstrip("vV"),
        "tag": latest,
        "available": parse_version(latest) > parse_version(current),
        "notes": rel.get("body") or "",
        "published": rel.get("published_at") or "",
        "zip_url": rel.get("zipball_url") or "",
        "repo": repo,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _managed_files(base: Path):
    for p in base.rglob("*"):
        rel = p.relative_to(base)
        if p.is_file() and rel.parts[0] not in PROTECTED and "__pycache__" not in rel.parts:
            yield rel


def write_manifest(data_dir: Path):
    """Ricorda com'era config.yaml appena installato, per capire se l'utente l'ha modificato."""
    (data_dir / ".manifest.json").write_text(json.dumps({"config.yaml": _sha(ROOT / "config.yaml"),
                                                          "version": version()}), encoding="utf-8")


def ensure_manifest(data_dir: Path):
    if not (data_dir / ".manifest.json").exists():
        write_manifest(data_dir)


def _preserve_config_edits(data_dir: Path, say):
    """Se config.yaml è stato modificato a mano, le modifiche vengono salvate in data/ prima di sostituirlo."""
    try:
        manifest = json.loads((data_dir / ".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    current = ROOT / "config.yaml"
    if manifest.get("config.yaml") and _sha(current) != manifest["config.yaml"]:
        keep = data_dir / f"config.modificato-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
        shutil.copy2(current, keep)
        say(f"config.yaml era stato modificato a mano: copia salvata in {keep.name}. "
            "Sposta le tue impostazioni in data/config.yaml (non viene mai sovrascritto).")
        user_cfg = data_dir / "config.yaml"
        body = user_cfg.read_text(encoding="utf-8") if user_cfg.exists() else ""
        if not any(line.strip() and not line.lstrip().startswith("#") for line in body.splitlines()):
            # data/config.yaml è ancora vuoto: le modifiche diventano le impostazioni dell'utente
            shutil.copy2(current, user_cfg)
            say("Le modifiche sono state applicate in data/config.yaml.")


def _backup(say) -> Path:
    bdir = ROOT / "runtime" / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    path = bdir / f"codice-v{version()}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in _managed_files(ROOT):
            z.write(ROOT / rel, rel.as_posix())
    say(f"Backup del codice attuale: runtime/backups/{path.name}")
    return path


def _install_tree(src: Path):
    """Copia i file della nuova versione. os.replace: uno script in esecuzione non viene corrotto."""
    for rel in _managed_files(src):
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".nuovo")
        shutil.copyfile(src / rel, tmp)
        try:
            shutil.copymode(src / rel, tmp)
        except OSError:
            pass
        os.replace(tmp, dst)


def _restore(backup: Path, say):
    with zipfile.ZipFile(backup) as z:
        tmpdir = ROOT / "runtime" / "update" / "ripristino"
        shutil.rmtree(tmpdir, ignore_errors=True)
        z.extractall(tmpdir)
    _install_tree(tmpdir)
    say("Ripristinata la versione precedente.")


def _pip_install(say):
    py = sys.executable
    reqs = [ROOT / "requirements.txt"]
    try:
        import nvidia.cublas  # noqa: F401  (se le librerie GPU erano installate, si aggiornano anche loro)
        reqs.append(ROOT / "requirements-gpu.txt")
    except ImportError:
        pass
    for r in reqs:
        if r.exists():
            say(f"Aggiorno i pacchetti Python ({r.name})...")
            res = subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(r)], capture_output=True, text=True)
            if res.returncode:
                raise RuntimeError(f"pip install fallito: {res.stderr[-800:]}")


def apply(cfg: dict, info: dict | None = None, say=print) -> dict:
    """Installa l'ultima versione. Ritorna {"ok": bool, "version": ..., "log": [...]}."""
    lines: list[str] = []

    def _say(msg):
        lines.append(msg)
        log.info("%s", msg)
        say(msg)

    data_dir = Path(cfg["_data_dir"])
    info = info or check(cfg)
    if not info["available"]:
        _say(f"Già aggiornato (versione {info['current']}).")
        return {"ok": True, "version": info["current"], "log": lines, "restart": False}

    _say(f"Aggiorno dalla versione {info['current']} alla {info['latest']}...")
    _preserve_config_edits(data_dir, _say)
    backup = _backup(_say)
    try:
        if (ROOT / ".git").is_dir() and shutil.which("git") and not (cfg.get("update") or {}).get("api"):
            _say("Installazione da Git...")
            for cmd in (["git", "fetch", "--tags", "--force", "origin"],
                        ["git", "-c", "advice.detachedHead=false", "checkout", "--force", info["tag"]]):
                res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
                if res.returncode:
                    raise RuntimeError(f"{' '.join(cmd)}: {res.stderr.strip()[-500:]}")
        else:
            _say("Scarico la nuova versione...")
            req = urllib.request.Request(info["zip_url"], headers={"User-Agent": "husky-locale-updater"})
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = r.read()
            staging = ROOT / "runtime" / "update" / "nuova"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                for name in z.namelist():  # niente percorsi pericolosi nello zip
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise RuntimeError(f"Percorso non valido nell'archivio: {name}")
                z.extractall(staging)
            tops = [p for p in staging.iterdir() if p.is_dir()]
            src = tops[0] if len(tops) == 1 and not (staging / "server.py").exists() else staging
            if not (src / "server.py").exists():
                raise RuntimeError("Archivio non valido: manca server.py")
            _say("Installo i file nuovi (data/, modelli e Ollama restano dove sono)...")
            _install_tree(src)
        _pip_install(_say)
        _selftest(_say)
    except Exception as e:
        _say(f"ERRORE: {e}")
        _restore(backup, _say)
        try:
            _pip_install(_say)  # rimette anche i pacchetti della versione precedente
        except Exception as e2:
            _say(f"Attenzione: pacchetti non ripristinati ({e2})")
        return {"ok": False, "version": info["current"], "log": lines, "restart": False}

    write_manifest(data_dir)
    # al riavvio la nuova versione deve confermarsi da sola; se non ci riesce si torna indietro (startup_check)
    _marker(data_dir).write_text(json.dumps({"backup": str(backup), "from": info["current"], "to": version(),
                                             "attempts": 0, "time": time.strftime("%Y-%m-%d %H:%M:%S")}),
                                 encoding="utf-8")
    _say(f"Installata la versione {version()}: provata con successo, ora riavvio. I tuoi dati sono intatti.")
    _say("Se dopo il riavvio la nuova versione non partisse, il server tornerà da solo alla versione precedente.")
    return {"ok": True, "version": version(), "log": lines, "restart": True}


# ------------------------------------------------------------------ verifica dopo l'aggiornamento
PENDING = ".update_pending.json"
MAX_FAILED_STARTS = 2


def _marker(data_dir: Path) -> Path:
    return Path(data_dir) / PENDING


def _selftest(say):
    """Prima di riavviare: la nuova versione deve caricarsi (moduli, configurazione, pagine, account)."""
    say("Provo la nuova versione prima di riavviare...")
    res = subprocess.run([sys.executable, "server.py", "--selftest"], cwd=ROOT, capture_output=True, text=True,
                         timeout=300, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if res.returncode or "SELFTEST OK" not in res.stdout:
        tail = (res.stdout + res.stderr).strip().splitlines()[-6:]
        raise RuntimeError("la nuova versione non si avvia:\n    " + "\n    ".join(tail))
    say("Prova superata.")


def startup_check(data_dir: Path):
    """Chiamato a ogni avvio. Se un aggiornamento non si è mai confermato dopo troppi avvii falliti,
    rimette la versione precedente e chiede un riavvio (codice 42)."""
    m = _marker(data_dir)
    if not m.exists():
        return
    try:
        info = json.loads(m.read_text(encoding="utf-8"))
    except ValueError:
        m.unlink(missing_ok=True)
        return
    info["attempts"] = int(info.get("attempts", 0)) + 1
    if info["attempts"] <= MAX_FAILED_STARTS:
        m.write_text(json.dumps(info), encoding="utf-8")
        log.warning("Avvio %d dopo l'aggiornamento alla %s: in attesa di conferma", info["attempts"], info.get("to"))
        return
    backup = Path(info.get("backup", ""))
    log.error("La versione %s non è riuscita a partire per %d volte: torno alla %s",
              info.get("to"), MAX_FAILED_STARTS, info.get("from"))
    m.unlink(missing_ok=True)
    if backup.exists():
        _restore(backup, lambda msg: log.error("%s", msg))
        try:
            _pip_install(lambda msg: log.warning("%s", msg))
        except Exception as e:
            log.error("Pacchetti non ripristinati: %s", e)
        write_manifest(Path(data_dir))
        (Path(data_dir) / "update_failed.json").write_text(json.dumps(info), encoding="utf-8")
        raise SystemExit(RESTART_CODE)
    log.error("Backup %s non trovato: impossibile tornare indietro automaticamente", backup)


def confirm_update(data_dir: Path):
    """La nuova versione è partita e funziona: l'aggiornamento è confermato."""
    m = _marker(data_dir)
    if m.exists():
        m.unlink(missing_ok=True)
        log.info("Aggiornamento confermato: la nuova versione funziona")


def main():
    import argparse

    from .config import load

    ap = argparse.ArgumentParser(description="Aggiornamento di Husky locale (conserva i dati)")
    ap.add_argument("--check", action="store_true", help="controlla soltanto")
    ap.add_argument("--yes", action="store_true", help="non chiedere conferma")
    ap.add_argument("-c", "--config", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    cfg = load(args.config)
    info = check(cfg)
    print(f"Versione installata: {info['current']}   ultima: {info['latest'] or '?'}")
    if not info["available"]:
        print("Nessun aggiornamento disponibile.")
        return
    if info["notes"]:
        print("\nNovità:\n" + info["notes"] + "\n")
    if args.check:
        return
    if not args.yes and input("Aggiornare adesso? I dati restano intatti. [s/N] ").strip().lower() not in ("s", "si", "sì", "y"):
        return
    res = apply(cfg, info, say=lambda m: print("  " + m))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
