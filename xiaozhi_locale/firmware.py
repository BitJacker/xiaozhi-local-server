"""Firmware da installare sugli ESP32 / ESP8266 direttamente dal browser (pagina Admin → Firmware).

L'admin carica un file .bin "completo" (merged / factory, da scrivere dall'indirizzo 0: es. quello che ESPHome
scarica con "Download → Factory format", o il merged-binary dei rilasci di xiaozhi-esp32) e sceglie il chip.
La pagina usa ESP Web Tools (incluso nel server, nessun file scaricato da Internet) e la porta USB del computer
tramite Web Serial: funziona con Chrome ed Edge, in HTTPS o su localhost.
I file stanno in data/firmware/<id>/ e si scaricano solo da un admin collegato.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import time
from pathlib import Path

from .auth import JsonStore

CHIPS = ["ESP32", "ESP32-S2", "ESP32-S3", "ESP32-C2", "ESP32-C3", "ESP32-C5", "ESP32-C6", "ESP32-H2", "ESP32-P4",
         "ESP8266"]
MAX_SIZE = 32 * 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{8}$")


class FirmwareStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "firmware"
        self.store = JsonStore(Path(data_dir) / "firmware.json")
        self.store.data.setdefault("items", [])

    def items(self) -> list[dict]:
        return self.store.data["items"]

    def get(self, fid: str) -> dict | None:
        return next((x for x in self.items() if x["id"] == fid), None) if ID_RE.match(fid or "") else None

    async def upload(self, reader) -> dict:
        """Legge il modulo multipart (nome, versione, chip, file) scrivendo il file su disco a pezzi."""
        fields: dict[str, str] = {}
        fid = secrets.token_hex(4)
        folder = self.dir / fid
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "firmware.bin"
        size, sha, head = 0, hashlib.sha256(), b""
        try:
            async for part in reader:
                if part.name == "file":
                    with open(tmp, "wb") as f:
                        while chunk := await part.read_chunk(256 * 1024):
                            size += len(chunk)
                            if size > MAX_SIZE:
                                raise ValueError("File troppo grande (massimo 32 MB)")
                            if len(head) < 0x1001:
                                head += chunk[:0x1001 - len(head)]
                            sha.update(chunk)
                            f.write(chunk)
                elif part.name in ("name", "version", "chip"):
                    fields[part.name] = (await part.text())[:80].strip()
            chip = fields.get("chip", "")
            if chip not in CHIPS:
                raise ValueError("Scegli il chip della scheda")
            if size < 4096:
                raise ValueError("Scegli il file .bin del firmware")
            # un firmware completo inizia con il bootloader (byte 0xE9), all'indirizzo 0 o 0x1000 (ESP32 classico)
            if head[:1] != b"\xe9" and head[0x1000:0x1001] != b"\xe9":
                raise ValueError("Questo non sembra un firmware completo da scrivere dall'indirizzo 0 "
                                 "(serve il file \"merged\" o \"factory\")")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        item = {"id": fid, "name": fields.get("name") or "Firmware", "version": fields.get("version") or "1",
                "chip": chip, "size": size, "sha256": sha.hexdigest(), "created": int(time.time())}
        (folder / "manifest.json").write_text(json.dumps(self.manifest(item), ensure_ascii=False), encoding="utf-8")
        with self.store.lock:
            self.store.data["items"].append(item)
            self.store.save()
        return item

    @staticmethod
    def manifest(item: dict) -> dict:
        return {"name": item["name"], "version": item["version"], "new_install_prompt_erase": True,
                "builds": [{"chipFamily": item["chip"], "parts": [{"path": "firmware.bin", "offset": 0}]}]}

    def path(self, fid: str, name: str) -> Path | None:
        if self.get(fid) is None or name not in ("manifest.json", "firmware.bin"):
            return None
        p = self.dir / fid / name
        return p if p.exists() else None

    def delete(self, fid: str) -> bool:
        if self.get(fid) is None:
            return False
        with self.store.lock:
            self.store.data["items"] = [x for x in self.items() if x["id"] != fid]
            self.store.save()
        shutil.rmtree(self.dir / fid, ignore_errors=True)
        return True
