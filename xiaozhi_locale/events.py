"""Registro degli eventi di sicurezza, visibile nella pagina Admin.

Registra chi entra, i tentativi falliti, le modifiche ad account e dispositivi, gli aggiornamenti e gli
strumenti che cambiano qualcosa (per esempio "impara questo volto"). Non registra mai il contenuto delle
conversazioni. Il file (data/events.jsonl) è cifrato come la cronologia e tiene solo gli ultimi eventi.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("events")

LABELS = {
    "login": "Accesso",
    "login_failed": "Password o codice sbagliati",
    "locked": "Account bloccato (troppi tentativi)",
    "admin_created": "Primo amministratore creato",
    "password": "Password cambiata",
    "2fa_on": "Verifica in due passaggi attivata",
    "2fa_off": "Verifica in due passaggi disattivata",
    "user": "Utenti",
    "device_approved": "Dispositivo approvato",
    "device_removed": "Dispositivo revocato",
    "device_rejected": "Attivazione rifiutata (troppe richieste)",
    "device_impostor": "Dispositivo con Client-Id diverso (possibile impersonazione)",
    "session_closed": "Connessione chiusa per revoca",
    "tool": "Strumento che modifica qualcosa",
    "update": "Aggiornamento",
}


class EventLog:
    def __init__(self, data_dir: Path, box=None, keep: int = 3000):
        self.path = Path(data_dir) / "events.jsonl"
        self.box = box
        self.keep = keep
        self._lock = threading.Lock()

    def add(self, kind: str, detail: str = "", user: str = "", ip: str = ""):
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "user": user or "", "ip": ip or "",
                 "detail": (detail or "")[:200]}
        line = json.dumps(entry, ensure_ascii=False)
        if self.box is not None:
            line = self.box.encrypt(line)
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._trim()
        except OSError as e:
            log.warning("Registro eventi non scritto: %s", e)

    def _trim(self):
        # si accorcia solo ogni tanto (quando supera di molto il limite), non a ogni riga
        if self.path.stat().st_size < self.keep * 400:
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) > self.keep:
            self.path.write_text("\n".join(lines[-self.keep:]) + "\n", encoding="utf-8")

    def recent(self, limit: int = 300) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                if self.box is not None:
                    line = self.box.decrypt(line)
                e = json.loads(line)
            except Exception:
                continue
            e["label"] = LABELS.get(e.get("kind"), e.get("kind"))
            out.append(e)
        return out
