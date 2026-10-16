"""Promemoria e note vocali, salvati sul server (cifrati), separati per utente.

"Ricordami di portare il libro di storia", "cosa devo ricordare?", "togli il promemoria del libro".
Niente esce dal computer: è un file in data/ come la cronologia.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .auth import JsonStore
from .tools import ToolSet

MAX_NOTES = 50


class Notes:
    def __init__(self, data_dir: Path, box=None):
        self.store = JsonStore(Path(data_dir) / "notes.json")
        self.box = box

    def _load(self, user: str) -> list[dict]:
        raw = self.store.data.get(user)
        if not raw:
            return []
        try:
            return json.loads(self.box.decrypt(raw) if self.box else raw)
        except Exception:
            return []

    def _save(self, user: str, notes: list[dict]):
        raw = json.dumps(notes, ensure_ascii=False)
        with self.store.lock:
            self.store.data[user] = self.box.encrypt(raw) if self.box else raw
            self.store.save()

    def add(self, user: str, text: str) -> str:
        text = " ".join(str(text).split())[:300]
        if not text:
            return "Errore: promemoria vuoto"
        notes = self._load(user)
        if len(notes) >= MAX_NOTES:
            return f"Hai già {MAX_NOTES} promemoria: togline qualcuno prima di aggiungerne altri."
        notes.append({"text": text, "time": time.strftime("%d/%m %H:%M")})
        self._save(user, notes)
        return f"Promemoria salvato ({len(notes)} in totale)."

    def list(self, user: str) -> str:
        notes = self._load(user)
        if not notes:
            return "Non ci sono promemoria."
        return "\n".join(f"{i}. {n['text']} (del {n['time']})" for i, n in enumerate(notes, 1))

    def remove(self, user: str, which: str) -> str:
        notes = self._load(user)
        which = str(which).strip()
        if which.lower() in ("tutto", "tutti"):
            self._save(user, [])
            return f"Tolti tutti i promemoria ({len(notes)})."
        if which.isdigit() and 1 <= int(which) <= len(notes):
            gone = notes.pop(int(which) - 1)
        else:
            match = [n for n in notes if which.lower() in n["text"].lower()]
            if not match:
                return f"Nessun promemoria contiene \"{which}\"."
            gone = match[0]
            notes.remove(gone)
        self._save(user, notes)
        return f"Tolto: {gone['text']}"

    def toolset(self, user: str) -> ToolSet:
        ts = ToolSet()

        async def add(_n, a):
            return self.add(user, a.get("testo", ""))

        async def lst(_n, _a):
            return self.list(user)

        async def rem(_n, a):
            return self.remove(user, a.get("numero_o_testo", ""))

        ts.add("promemoria_aggiungi", "Salva un promemoria o una nota dell'utente (es. 'ricordami di...').",
               {"type": "object", "properties": {"testo": {"type": "string", "description": "Cosa ricordare",
                                                           "maxLength": 300}}, "required": ["testo"]}, add)
        ts.add("promemoria_elenco", "Legge i promemoria e le note salvate dell'utente.",
               {"type": "object", "properties": {}}, lst)
        ts.add("promemoria_rimuovi", "Toglie un promemoria (numero, parte del testo, oppure 'tutto').",
               {"type": "object", "properties": {"numero_o_testo": {"type": "string", "maxLength": 100}},
                "required": ["numero_o_testo"]}, rem)
        return ts
