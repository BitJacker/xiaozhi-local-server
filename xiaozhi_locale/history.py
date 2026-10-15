"""Cronologia delle conversazioni, salvata SOLO sul disco del server (mai inviata a Internet).

Un file JSON Lines per giorno: data/history/AAAA-MM-GG.jsonl
Ogni riga: {"ts": "...", "device": "...", "session": "...", "role": "user|assistant", "text": "..."}
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
    def __init__(self, cfg: dict, root: Path):
        self.enabled = bool(cfg.get("enabled", True))
        self.dir = (root / cfg.get("dir", "data/history")).resolve()
        self.keep_days = int(cfg.get("keep_days", 0))  # 0 = per sempre
        self._lock = threading.Lock()
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._cleanup()
            log.info("Cronologia attiva: %s (solo su questo computer)", self.dir)

    def append(self, device: str, session: str, role: str, text: str):
        if not self.enabled or not text:
            return
        now = time.localtime()
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", now), "device": device,
               "session": session, "role": role, "text": text}
        path = self.dir / f"{time.strftime('%Y-%m-%d', now)}.jsonl"
        with self._lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

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
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
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

    def _cleanup(self):
        """Cancella i giorni più vecchi di keep_days (se impostato)."""
        if self.keep_days <= 0:
            return
        limit = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.keep_days * 86400))
        for day in self.days():
            if day < limit:
                self.delete(day)
