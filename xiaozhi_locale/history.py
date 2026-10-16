"""Cronologia delle conversazioni, salvata SOLO sul disco del server (mai inviata a Internet).

Un file JSON Lines per giorno: data/history/AAAA-MM-GG.jsonl
Ogni riga: {"ts": "...", "user": "...", "device": "...", "session": "...", "role": "user|assistant", "text": "..."}
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("history")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class History:
    def __init__(self, cfg: dict, data_dir: Path, box=None):
        self.enabled = bool(cfg.get("enabled", True))
        self.dir = (Path(data_dir) / "history").resolve()
        self.keep_days = int(cfg.get("keep_days", 30))  # 0 = per sempre
        self.box = box  # cifratura AES-GCM delle righe (cryptobox.Box), se attiva
        self.encrypted = bool(box and box.enabled)
        self._lock = threading.Lock()
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.cleanup()
            log.info("Cronologia attiva: %s (solo su questo computer)", self.dir)

    def append(self, user: str, device: str, session: str, role: str, text: str):
        if not self.enabled or not text:
            return
        now = time.localtime()
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", now), "user": user, "device": device,
               "session": session, "role": role, "text": text}
        path = self.dir / f"{time.strftime('%Y-%m-%d', now)}.jsonl"
        line = json.dumps(rec, ensure_ascii=False)
        if self.encrypted:
            line = self.box.encrypt(line)
        with self._lock, open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def days(self) -> list[str]:
        if not self.enabled:
            return []
        return sorted((p.stem for p in self.dir.glob("*.jsonl") if _DAY_RE.match(p.stem)), reverse=True)

    def read(self, day: str) -> list[dict]:
        if not self.enabled or not _DAY_RE.match(day or ""):
            return []  # niente percorsi arbitrari: solo nomi di giorno validi
        path = self.dir / f"{day}.jsonl"
        if not path.exists():
            return []
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if self.box:
                        line = self.box.decrypt(line)  # le righe in chiaro (versioni precedenti) restano leggibili
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def delete(self, day: str) -> bool:
        if not self.enabled or not _DAY_RE.match(day or ""):
            return False
        path = self.dir / f"{day}.jsonl"
        if path.exists():
            path.unlink()
            return True
        return False

    def cleanup(self):
        """Cancella i giorni più vecchi di keep_days (se impostato)."""
        if self.keep_days <= 0:
            return
        limit = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.keep_days * 86400))
        for day in self.days():
            if day < limit:
                self.delete(day)
