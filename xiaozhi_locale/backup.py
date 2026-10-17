"""Backup automatico e cifrato della cartella data/ (utenti, dispositivi, cronologia, documenti, impostazioni...).

* Si accende dalla pagina Admin (sezione Backup), dove si scelgono l'ora, quante copie tenere, la cartella (anche
  una chiavetta o un disco di rete) e la password del backup.
* Ogni copia è un unico file .husky-backup cifrato con AES-256-GCM, a pezzi da 1 MB; la chiave viene dalla password
  (scrypt). Senza la password il file non si legge: si può tenere anche fuori dal server.
* La musica e i firmware non entrano nel backup (sono file grandi che si possono ricaricare).
* Per ripristinare (a server spento):
      python -m xiaozhi_locale.backup restore FILE [--data CARTELLA]
  chiede la password e rimette i dati; quelli di prima restano in una cartella data.prima-del-ripristino-...
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import io
import logging
import os
import re
import secrets
import shutil
import struct
import sys
import time
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("backup")
MAGIC = b"HUSKYBK1"
CHUNK = 1 << 20
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
NAME_RE = re.compile(r"^husky-\d{8}-\d{6}\.husky-backup$")
SKIP_DIRS = {"music", "firmware", "__pycache__"}
SKIP_SUFFIX = (".part", ".tmp")


def derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)


class EncryptWriter(io.RawIOBase):
    """File di sola scrittura che cifra a pezzi (ogni pezzo ha il suo nonce e il suo numero; l'ultimo è segnato,
    così un file troncato o con pezzi scambiati non si decifra)."""

    def __init__(self, out, key: bytes, salt: bytes):
        self.out, self.aes, self.buf, self.index = out, AESGCM(key), bytearray(), 0
        out.write(MAGIC + salt)

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        while len(self.buf) >= CHUNK:
            self._emit(bytes(self.buf[:CHUNK]), False)
            del self.buf[:CHUNK]
        return len(b)

    def _emit(self, data: bytes, final: bool):
        nonce = secrets.token_bytes(12)
        ct = self.aes.encrypt(nonce, data, struct.pack(">Q?", self.index, final))
        self.out.write(struct.pack(">I", len(ct)) + nonce + ct)
        self.index += 1

    def close(self):
        if not self.closed:
            self._emit(bytes(self.buf), True)
            self.buf.clear()
        super().close()


def decrypt_file(src: Path, dst, password: str):
    with open(src, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("Non è un backup di Husky")
        key = derive(password, f.read(16))
        aes, index = AESGCM(key), 0
        while True:
            head = f.read(4)
            if len(head) < 4:
                raise ValueError("Backup incompleto (file troncato)")
            n = struct.unpack(">I", head)[0]
            nonce, ct = f.read(12), f.read(n)
            data = None
            for final in (False, True):
                try:
                    data = aes.decrypt(nonce, ct, struct.pack(">Q?", index, final))
                    break
                except Exception:
                    continue
            if data is None:
                raise ValueError("Password sbagliata o backup danneggiato")
            dst.write(data)
            index += 1
            if final:
                return


class Backup:
    def __init__(self, app, data_dir: Path, box):
        from .auth import JsonStore
        from .config import ROOT
        self.app = app
        self.data_dir = Path(data_dir).resolve()
        self.box = box
        self.root = ROOT
        self.store = JsonStore(self.data_dir / "backup.json")
        d = self.store.data
        d.setdefault("enabled", False)
        d.setdefault("time", "02:30")
        d.setdefault("keep", 7)
        d.setdefault("folder", "backups")
        self.last_ok: float | None = d.get("last_ok")
        self.running = False

    # ------------------------------------------------------------------ impostazioni
    @property
    def folder(self) -> Path:
        f = Path(self.store.data.get("folder") or "backups")
        return (f if f.is_absolute() else self.root / f).resolve()

    def _key(self) -> tuple[bytes, bytes] | None:
        d = self.store.data
        if not d.get("key"):
            return None
        key = bytes.fromhex(self.box.decrypt(d["key"]) if self.box else d["key"])
        return key, bytes.fromhex(d["salt"])

    def status(self) -> dict:
        d = self.store.data
        return {"enabled": d["enabled"], "time": d["time"], "keep": d["keep"], "folder": d["folder"],
                "folder_path": str(self.folder), "has_password": bool(d.get("key")), "running": self.running,
                "last_ok": d.get("last_ok_text", ""), "last_error": d.get("last_error", ""), "files": self.files()}

    def configure(self, d: dict):
        t = str(d.get("time", self.store.data["time"]))
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ValueError("Ora non valida (es. 02:30)")
        keep = int(d.get("keep", self.store.data["keep"]))
        if not 1 <= keep <= 365:
            raise ValueError("Copie da tenere: da 1 a 365")
        folder = str(d.get("folder", self.store.data["folder"])).strip() or "backups"
        if len(folder) > 300 or "\x00" in folder:
            raise ValueError("Cartella non valida")
        fp = (Path(folder) if Path(folder).is_absolute() else self.root / folder).resolve()
        if fp == self.data_dir or self.data_dir in fp.parents:
            raise ValueError("La cartella dei backup non può stare dentro la cartella dei dati")
        enabled = bool(d.get("enabled", self.store.data["enabled"]))
        if enabled and not self.store.data.get("key"):
            raise ValueError("Prima scegli la password del backup")
        with self.store.lock:
            self.store.data.update(time=t, keep=keep, folder=folder, enabled=enabled)
            self.store.save()

    def set_password(self, password: str):
        if len(password) < 10:
            raise ValueError("La password del backup deve avere almeno 10 caratteri")
        salt = secrets.token_bytes(16)
        key = derive(password, salt)
        with self.store.lock:
            self.store.data["salt"] = salt.hex()
            self.store.data["key"] = self.box.encrypt(key.hex()) if self.box else key.hex()
            self.store.save()

    # ------------------------------------------------------------------ copie
    def files(self) -> list[dict]:
        if not self.folder.is_dir():
            return []
        out = [{"name": p.name, "size": p.stat().st_size,
                "time": time.strftime("%d/%m/%Y %H:%M", time.localtime(p.stat().st_mtime))}
               for p in self.folder.iterdir() if p.is_file() and NAME_RE.match(p.name)]
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def file_path(self, name: str) -> Path | None:
        if not NAME_RE.match(name or ""):
            return None
        p = self.folder / name
        return p if p.is_file() else None

    def delete(self, name: str) -> bool:
        p = self.file_path(name)
        if p is None:
            return False
        p.unlink()
        return True

    def _members(self):
        for dirpath, dirnames, filenames in os.walk(self.data_dir):
            rel_dir = Path(dirpath).relative_to(self.data_dir)
            dirnames[:] = [d for d in dirnames if not (rel_dir == Path(".") and d in SKIP_DIRS)
                           and Path(dirpath, d).resolve() != self.folder]
            for fn in filenames:
                if not fn.endswith(SKIP_SUFFIX):
                    yield Path(dirpath, fn), (rel_dir / fn).as_posix()

    def create(self) -> dict:
        """Crea una copia (bloccante: si chiama in un thread)."""
        keys = self._key()
        if keys is None:
            raise ValueError("Prima scegli la password del backup")
        key, salt = keys
        self.folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("husky-%Y%m%d-%H%M%S.husky-backup")
        tmp = self.folder / (name + ".part")
        count = 0
        with open(tmp, "wb") as raw:
            w = EncryptWriter(raw, key, salt)
            with zipfile.ZipFile(w, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                z.writestr("HUSKY_BACKUP.txt", f"Backup di Husky locale, {time.strftime('%d/%m/%Y %H:%M')}\n")
                for path, arc in self._members():
                    try:
                        z.write(path, "data/" + arc)
                        count += 1
                    except OSError as e:  # file in uso o sparito nel frattempo: si va avanti
                        log.warning("Backup: salto %s (%s)", arc, e)
            w.close()
        os.replace(tmp, self.folder / name)
        self._prune()
        return {"name": name, "files": count, "size": (self.folder / name).stat().st_size}

    def _prune(self):
        for old in self.files()[int(self.store.data["keep"]):]:
            (self.folder / old["name"]).unlink(missing_ok=True)

    async def run_now(self) -> dict:
        if self.running:
            raise ValueError("Backup già in corso")
        self.running = True
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, self.create)
            self.last_ok = time.time()
            with self.store.lock:
                self.store.data.update(last_ok=self.last_ok, last_ok_text=time.strftime("%d/%m/%Y %H:%M"), last_error="")
                self.store.save()
            log.info("Backup creato: %s (%d file)", res["name"], res["files"])
            return res
        except Exception as e:
            with self.store.lock:
                self.store.data["last_error"] = f"{time.strftime('%d/%m/%Y %H:%M')}: {e}"[:300]
                self.store.save()
            raise
        finally:
            self.running = False

    async def loop(self):
        """Ogni minuto: se il backup è acceso ed è l'ora scelta, crea la copia del giorno."""
        while True:
            await asyncio.sleep(60 - time.time() % 60 + 1)
            d = self.store.data
            if not d.get("enabled") or time.strftime("%H:%M") != d.get("time") or d.get("last_day") == time.strftime("%Y-%m-%d"):
                continue
            with self.store.lock:
                d["last_day"] = time.strftime("%Y-%m-%d")
                self.store.save()
            try:
                await self.run_now()
                self.app["events"].add("backup", "backup automatico creato")
            except Exception as e:
                log.error("Backup automatico non riuscito: %s", e)
                self.app["events"].add("backup", f"backup automatico NON riuscito: {e}"[:200])


def restore(src: Path, data_dir: Path, password: str) -> Path | None:
    """Rimette i dati di un backup in data_dir. Ritorna dove sono finiti i dati che c'erano prima."""
    data_dir = Path(data_dir).resolve()
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = data_dir.parent / f".husky-restore-{secrets.token_hex(4)}.zip"
    try:
        with open(tmp_zip, "wb") as out:
            decrypt_file(src, out, password)
        with zipfile.ZipFile(tmp_zip) as z:
            names = z.namelist()
            if "HUSKY_BACKUP.txt" not in names:
                raise ValueError("Il file non contiene un backup di Husky")
            for n in names:  # niente percorsi che escono dalla cartella dei dati
                if n != "HUSKY_BACKUP.txt" and (not n.startswith("data/") or ".." in Path(n).parts or Path(n).is_absolute()):
                    raise ValueError(f"Percorso non valido nel backup: {n}")
            staging = data_dir.parent / f".husky-restore-{secrets.token_hex(4)}"
            for n in names:
                if n.startswith("data/") and not n.endswith("/"):
                    target = staging / n[len("data/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(n) as fsrc, open(target, "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst)
        old = None
        if data_dir.exists() and any(data_dir.iterdir()):
            old = data_dir.with_name(data_dir.name + time.strftime(".prima-del-ripristino-%Y%m%d-%H%M%S"))
            data_dir.rename(old)
            # musica e firmware non sono nel backup: si tengono quelli che c'erano
            for keep in SKIP_DIRS - {"__pycache__"}:
                if (old / keep).is_dir():
                    shutil.copytree(old / keep, staging / keep, dirs_exist_ok=True)
        staging.rename(data_dir)
        return old
    finally:
        tmp_zip.unlink(missing_ok=True)


def main(argv=None):
    from . import config as config_mod
    ap = argparse.ArgumentParser(prog="python -m xiaozhi_locale.backup", description="Backup cifrati di Husky")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore", help="rimette i dati da un backup (a server spento)")
    r.add_argument("file")
    r.add_argument("--data", help="cartella dei dati (predefinita: quella della configurazione)")
    r.add_argument("--password-env", help="legge la password da questa variabile d'ambiente (script)")
    v = sub.add_parser("verify", help="controlla che il backup si legga con la password")
    v.add_argument("file")
    v.add_argument("--password-env")
    args = ap.parse_args(argv)
    pw = os.environ.get(args.password_env, "") if args.password_env else getpass.getpass("Password del backup: ")
    if args.cmd == "verify":
        out = io.BytesIO()
        decrypt_file(Path(args.file), out, pw)
        with zipfile.ZipFile(out) as z:
            bad = z.testzip()
            print(f"Backup leggibile: {len(z.namelist())} file" + (f", file danneggiato: {bad}" if bad else ""))
        return 1 if bad else 0
    data_dir = Path(args.data) if args.data else Path(config_mod.load()["_data_dir"])
    try:
        old = restore(Path(args.file), data_dir, pw)
    except ValueError as e:
        print(f"Ripristino non riuscito: {e}")
        return 1
    print(f"Dati ripristinati in {data_dir}.")
    if old:
        print(f"I dati di prima sono in {old} (cancellali quando hai controllato che tutto funzioni).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
