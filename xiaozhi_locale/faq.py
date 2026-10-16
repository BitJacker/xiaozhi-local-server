"""Risposte immediate a domande frequenti, scritte dall'admin (orari, regole della classe, date delle verifiche).

Se la domanda (anche detta a voce, con piccoli errori di trascrizione) assomiglia abbastanza a una di quelle
scritte, Husky risponde subito con la risposta fissa, senza chiedere al modello: più veloce e sempre giusta.
"""
from __future__ import annotations

import difflib
import re
import secrets
import unicodedata
from pathlib import Path

from .auth import JsonStore


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))  # è -> e
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    # media di somiglianza delle lettere (errori di trascrizione) e delle parole in comune: così "a che ora
    # finisce la lezione" NON viene scambiata per "a che ora inizia la lezione" (meglio nessuna risposta fissa
    # che quella sbagliata: in quel caso risponde il modello)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()), set(b.split())
    words = len(wa & wb) / len(wa | wb)
    return (ratio + words) / 2


class FAQ:
    def __init__(self, data_dir: Path, min_similarity: float = 0.82):
        self.store = JsonStore(Path(data_dir) / "faq.json")
        self.store.data.setdefault("items", [])
        self.min_similarity = min_similarity

    def items(self) -> list[dict]:
        return self.store.data["items"]

    def save(self, d: dict) -> dict:
        questions = [q.strip() for q in d.get("questions") or [] if str(q).strip()][:20]
        answer = str(d.get("answer", "")).strip()
        if not questions or not answer:
            raise ValueError("Scrivi almeno una domanda e la risposta")
        if len(answer) > 800:
            raise ValueError("Risposta troppo lunga (massimo 800 caratteri): viene letta ad alta voce")
        item = {"id": d.get("id") or secrets.token_hex(4), "questions": questions, "answer": answer}
        with self.store.lock:
            items = self.store.data["items"]
            idx = next((i for i, x in enumerate(items) if x["id"] == item["id"]), None)
            if idx is None:
                items.append(item)
            else:
                items[idx] = item
            self.store.save()
        return item

    def delete(self, item_id: str) -> bool:
        with self.store.lock:
            before = len(self.store.data["items"])
            self.store.data["items"] = [x for x in self.store.data["items"] if x["id"] != item_id]
            self.store.save()
            return len(self.store.data["items"]) < before

    def match(self, text: str) -> tuple[str, float] | None:
        """La risposta fissa più adatta alla domanda, se abbastanza simile; altrimenti None."""
        best, score = None, 0.0
        for item in self.store.data["items"]:
            for q in item["questions"]:
                s = similarity(text, q)
                if s > score:
                    best, score = item, s
        return (best["answer"], score) if best and score >= self.min_similarity else None
