"""Memoria a lungo termine per utente: le cose importanti che Husky ricorda tra una conversazione e l'altra.

È il modello a decidere cosa ricordare (strumento "memoria_salva", es. "mi chiamo Luca", "preparo l'esame di
biologia"); i ricordi dell'utente vengono aggiunti alle istruzioni all'inizio di ogni conversazione. Ognuno vede e
cancella i propri ricordi nella pagina Account. Salvati cifrati in data/memory.json; si può spegnere con
memory.enabled: false e decidere per quali ruoli vale (di serie non per i dispositivi, che sono condivisi).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path

from .auth import JsonStore
from .tools import ToolSet

MAX_FACTS = 30
log = logging.getLogger("memory")
PERSONAL_RE = re.compile(r"\b(mi chiamo|il mio nome|chiamami|ricorda|ricordati|ricordatelo|sono (un|una|al|in|di)|"
                         r"studio|frequento|preparo|sto preparando|mi piace|mi piacciono|preferisco|odio|non mi piace|"
                         r"il mio|la mia|i miei|le mie|ho \d+ anni|abito|vivo a)\b", re.I)
EXTRACT_PROMPT = ("Dal messaggio dell'utente estrai solo le informazioni su di lui che vale la pena ricordare a lungo "
                  "(nome, età, cosa studia, interessi, preferenze, esigenze). Scrivi una riga per informazione, breve e "
                  "in terza persona (es. \"Si chiama Ada\", \"Frequenta la terza B\"). Se non c'è niente da ricordare scrivi solo NESSUNA.\n\n"
                  "Messaggio: {text}")


class Memory:
    def __init__(self, data_dir: Path, box=None, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.store = JsonStore(Path(data_dir) / "memory.json")
        self.box = box

    def enabled_for(self, role: str) -> bool:
        return bool(self.cfg.get("enabled", True)) and role in self.cfg.get("roles", ["admin", "user"])

    def load(self, user: str) -> list[dict]:
        raw = self.store.data.get(user)
        if not raw:
            return []
        try:
            return json.loads(self.box.decrypt(raw) if self.box else raw)
        except Exception:
            return []

    def _save(self, user: str, facts: list[dict]):
        raw = json.dumps(facts, ensure_ascii=False)
        with self.store.lock:
            if facts:
                self.store.data[user] = self.box.encrypt(raw) if self.box else raw
            else:
                self.store.data.pop(user, None)
            self.store.save()

    def add(self, user: str, text: str) -> str:
        text = " ".join(str(text).split())[:200]
        if len(text) < 3:
            return "Errore: niente da ricordare"
        facts = self.load(user)
        if any(f["text"].lower().rstrip(".") == text.lower().rstrip(".") for f in facts):
            return "Lo sapevo già."
        if len(facts) >= MAX_FACTS:
            facts.pop(0)  # si dimentica il ricordo più vecchio
        facts.append({"id": secrets.token_hex(3), "text": text, "time": time.strftime("%d/%m/%Y")})
        self._save(user, facts)
        return "Ricordato."

    def forget(self, user: str, which: str) -> str:
        facts = self.load(user)
        which = str(which).strip().lower()
        keep = [f for f in facts if which not in f["text"].lower() and f["id"] != which]
        if len(keep) == len(facts):
            return "Non trovo un ricordo così."
        self._save(user, keep)
        return f"Dimenticato ({len(facts) - len(keep)})."

    def delete(self, user: str, fid: str | None = None) -> list[dict]:
        """Dalla pagina Account: un ricordo (fid) o tutti."""
        self._save(user, [f for f in self.load(user) if fid and f["id"] != fid])
        return self.load(user)

    def prompt(self, user: str) -> str:
        facts = self.load(user)
        if not facts:
            return ""
        # "certe, dette da lui": il prompt base chiede di non dare per scontato il nome dell'utente
        return ("Informazioni certe su questo utente, che ti ha detto lui nelle conversazioni passate (usale): "
                + "; ".join(f["text"] for f in facts) + ".")

    async def extract(self, llm, user: str, text: str):
        """Dopo la risposta, in sottofondo: se la frase sembra dire qualcosa di personale, il modello estrae cosa
        ricordare. Si chiede solo per queste frasi (non rallenta le altre risposte)."""
        if not PERSONAL_RE.search(text or ""):
            return
        try:
            out = await llm.complete(EXTRACT_PROMPT.format(text=text[:500]), 120)
        except Exception as e:
            log.debug("estrazione dei ricordi non riuscita: %s", e)
            return
        for line in out.splitlines():
            fact = line.strip().lstrip("-•* ").strip()
            if 4 <= len(fact) <= 160 and not fact.upper().startswith("NESSUN"):
                self.add(user, fact)

    def toolset(self, user: str) -> ToolSet:
        ts = ToolSet()

        async def save(_n, a):
            return self.add(user, a.get("fatto", ""))

        async def forget(_n, a):
            return self.forget(user, a.get("fatto", ""))
        ts.add("memoria_salva", "Ricorda per sempre una cosa importante sull'utente (nome, interessi, cosa sta "
               "studiando, preferenze), anche nelle prossime conversazioni. Usalo quando l'utente dice qualcosa di sé "
               "che vale la pena ricordare o chiede di ricordarlo.",
               {"type": "object", "properties": {"fatto": {"type": "string", "maxLength": 200,
                                                           "description": "la cosa da ricordare, in breve"}},
                "required": ["fatto"]}, save)
        ts.add("memoria_dimentica", "Dimentica una cosa che ricordavi sull'utente, se lui lo chiede.",
               {"type": "object", "properties": {"fatto": {"type": "string", "maxLength": 100}},
                "required": ["fatto"]}, forget)
        return ts
