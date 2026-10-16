"""Modalità lezione: Husky ascolta e trascrive la lezione senza rispondere, poi prepara un riassunto
(argomento, punti chiave, compiti).

Si avvia dalla pagina dell'assistente (pulsante) o a voce ("inizia la modalità lezione": strumento lezione_inizia,
è il modello a capirlo); si ferma con il pulsante o dicendo "fine della lezione". Trascrizione e riassunto restano
sul server, cifrati (data/lessons/), e si rileggono nella pagina Lezioni: ognuno vede le proprie, l'admin tutte.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from .auth import JsonStore

STOP_RE = re.compile(r"\b(fine|finisce|termina|terminare|chiudi|chiudere|stop|ferma)\b.{0,25}\blezione\b"
                     r"|\blezione\b.{0,15}\b(finita|terminata|conclusa)\b", re.I)
PIECE = 5000          # caratteri per ogni parte da riassumere (il modello piccolo ha una memoria limitata)
MAX_SECONDS = 3 * 3600

PROMPT_PART = ("Questa è una parte della trascrizione automatica di una lezione (può contenere errori di "
               "riconoscimento). Elenca in italiano, in punti brevi, gli argomenti spiegati, le definizioni e gli "
               "esempi importanti, e ogni compito, scadenza o verifica annunciata. Non inventare niente.\n\n"
               "TRASCRIZIONE:\n{text}")
PROMPT_FINAL = ("Ecco {what} di una lezione (dalla trascrizione automatica, può contenere errori). Scrivi in italiano, "
                "senza inventare, esattamente in questo formato:\nArgomento: (una riga)\nPunti chiave:\n- ...\n"
                "Compiti e scadenze:\n- ... (oppure \"nessuno\")\n\n{text}")


def is_stop(text: str) -> bool:
    return bool(STOP_RE.search(text or ""))


class Lessons:
    def __init__(self, data_dir: Path, box=None, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.dir = Path(data_dir) / "lessons"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = JsonStore(Path(data_dir) / "lessons.json")
        self.store.data.setdefault("items", [])
        self.box = box

    def allowed(self, role: str) -> bool:
        return role in self.cfg.get("roles", ["admin", "device"])

    @staticmethod
    def new(user: str, device: str, title: str = "") -> dict:
        now = time.time()
        return {"id": secrets.token_hex(5), "title": " ".join(str(title).split())[:100]
                or time.strftime("Lezione del %d/%m/%Y %H:%M", time.localtime(now)),
                "user": user, "device": device, "start": now, "parts": []}

    # ------------------------------------------------------------------ riassunto
    async def summarize(self, llm, transcript: str) -> str:
        transcript = transcript.strip()
        if len(transcript) < 80:
            return "Lezione troppo breve per un riassunto."
        if len(transcript) <= PIECE:
            return await llm.complete(PROMPT_FINAL.format(what="la trascrizione", text=transcript), 700)
        notes = []
        for i in range(0, len(transcript), PIECE):  # lezioni lunghe: prima ogni parte, poi l'insieme
            notes.append(await llm.complete(PROMPT_PART.format(text=transcript[i:i + PIECE]), 500))
        joined = "\n\n".join(f"Parte {n}:\n{t}" for n, t in enumerate(notes, 1))
        return await llm.complete(PROMPT_FINAL.format(what="gli appunti, parte per parte,", text=joined[:12000]), 800)

    # ------------------------------------------------------------------ archivio
    def save(self, lesson: dict, summary: str) -> dict:
        end = time.time()
        transcript = "\n".join(p["text"] for p in lesson["parts"])
        content = {"transcript": lesson["parts"], "summary": summary}
        raw = json.dumps(content, ensure_ascii=False)
        (self.dir / f"{lesson['id']}.json").write_text(self.box.encrypt(raw) if self.box else raw, encoding="utf-8")
        item = {"id": lesson["id"], "title": lesson["title"], "user": lesson["user"], "device": lesson["device"],
                "start": int(lesson["start"]), "end": int(end), "words": len(transcript.split())}
        with self.store.lock:
            self.store.data["items"] = [x for x in self.store.data["items"] if x["id"] != item["id"]] + [item]
            self.store.save()
        return item

    def list(self, user: str | None) -> list[dict]:
        items = [x for x in self.store.data["items"] if user is None or x["user"] == user]
        return sorted(items, key=lambda x: -x["start"])

    def get(self, lid: str, user: str | None) -> dict | None:
        item = next((x for x in self.store.data["items"] if x["id"] == lid), None)
        if item is None or (user is not None and item["user"] != user):
            return None
        try:
            raw = (self.dir / f"{lid}.json").read_text(encoding="utf-8")
            content = json.loads(self.box.decrypt(raw) if self.box else raw)
        except Exception:
            content = {"transcript": [], "summary": ""}
        return {**item, **content}

    def delete(self, lid: str, user: str | None) -> bool:
        if self.get(lid, user) is None:
            return False
        with self.store.lock:
            self.store.data["items"] = [x for x in self.store.data["items"] if x["id"] != lid]
            self.store.save()
        (self.dir / f"{lid}.json").unlink(missing_ok=True)
        return True
