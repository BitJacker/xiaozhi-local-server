"""Più assistenti con personalità diverse (es. "tutor di inglese", "assistente di laboratorio").

Ogni personaggio ha le sue istruzioni, la sua voce (o velocità), la lingua in cui ascolta e gli strumenti che può
usare. Si sceglie per dispositivo (pagina Admin), dalla pagina dell'assistente, oppure a voce ("passa al tutor di
inglese": strumento cambia_assistente, è il modello a capirlo). Impostazioni in data/personas.json.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from .auth import JsonStore

LANGS = ("it", "en", "fr", "de", "es", "auto")


class Personas:
    def __init__(self, data_dir: Path):
        self.store = JsonStore(Path(data_dir) / "personas.json")
        self.store.data.setdefault("items", [])
        self.store.data.setdefault("devices", {})

    def items(self) -> list[dict]:
        return self.store.data["items"]

    def get(self, pid: str | None) -> dict | None:
        return next((p for p in self.items() if p["id"] == pid), None) if pid else None

    def find(self, name: str) -> dict | None:
        """Per nome, anche detto a voce in modo approssimativo ("il tutor d'inglese")."""
        n = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
        words = set(n.split())
        best, score = None, 0
        for p in self.items():
            pn = set(re.sub(r"[^a-z0-9 ]", " ", p["name"].lower()).split())
            common = len(words & pn)
            if common > score:
                best, score = p, common
        return best

    def for_device(self, device_id: str) -> dict | None:
        return self.get(self.store.data["devices"].get((device_id or "").lower()))

    def save(self, d: dict, voices: list[str]) -> dict:
        pid = str(d.get("id") or secrets.token_hex(4))
        name = " ".join(str(d.get("name", "")).split())[:40]
        prompt = str(d.get("prompt", "")).strip()[:4000]
        if len(name) < 2:
            raise ValueError("Scrivi il nome del personaggio")
        if len(prompt) < 10:
            raise ValueError("Scrivi le istruzioni del personaggio (chi è, come risponde, in che lingua)")
        if any(p["name"].lower() == name.lower() and p["id"] != pid for p in self.items()):
            raise ValueError("C'è già un personaggio con questo nome")
        voice = str(d.get("voice") or "")
        if voice and voice not in voices:
            raise ValueError("Voce non disponibile sul server")
        lang = str(d.get("language") or "it")
        if lang not in LANGS:
            raise ValueError("Lingua non valida")
        try:
            speed = round(float(d.get("speed") or 1.0), 2)
            assert 0.5 <= speed <= 2.0
        except (ValueError, AssertionError):
            raise ValueError("Velocità della voce tra 0,5 e 2") from None
        groups = [str(g)[:40] for g in d.get("groups") or [] if str(g).strip()][:30]
        p = {"id": pid, "name": name, "prompt": prompt, "voice": voice, "speed": speed, "language": lang,
             "groups": groups}
        with self.store.lock:
            self.store.data["items"] = [x for x in self.items() if x["id"] != pid] + [p]
            self.store.save()
        return p

    def delete(self, pid: str) -> bool:
        before = len(self.items())
        with self.store.lock:
            self.store.data["items"] = [x for x in self.items() if x["id"] != pid]
            self.store.data["devices"] = {k: v for k, v in self.store.data["devices"].items() if v != pid}
            self.store.save()
        return len(self.items()) < before

    def assign(self, device_id: str, pid: str | None):
        if pid and self.get(pid) is None:
            raise ValueError("Personaggio inesistente")
        with self.store.lock:
            if pid:
                self.store.data["devices"][device_id.lower()] = pid
            else:
                self.store.data["devices"].pop(device_id.lower(), None)
            self.store.save()

    def public(self) -> list[dict]:
        return [{"id": p["id"], "name": p["name"], "language": p["language"]} for p in self.items()]
