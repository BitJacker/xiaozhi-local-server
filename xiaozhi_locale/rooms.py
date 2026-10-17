"""Più dispositivi nella stessa aula: risponde uno solo.

L'admin raggruppa i dispositivi per aula (pagina Admin → Aule). Quando dopo la parola di attivazione più dispositivi
della stessa aula sentono la stessa domanda, aspettano un attimo (window_ms, di serie 600 ms) e risponde solo quello
che l'ha sentita più forte, cioè il più vicino a chi parla. Gli altri chiudono l'ascolto senza dire niente e tornano
in attesa della parola di attivazione. Impostazioni in data/rooms.json.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from pathlib import Path

from .auth import JsonStore


class Rooms:
    def __init__(self, data_dir: Path):
        self.store = JsonStore(Path(data_dir) / "rooms.json")
        self.store.data.setdefault("rooms", [])
        self.store.data.setdefault("window_ms", 600)
        self._open: dict[str, dict] = {}  # gare in corso per aula: {"until", "claims": [(punteggio, id, futuro)]}
        self.stats = {"contests": 0, "silenced": 0}

    def items(self) -> list[dict]:
        return self.store.data["rooms"]

    @property
    def window(self) -> float:
        return int(self.store.data.get("window_ms", 600)) / 1000

    def room_of(self, device_id: str) -> dict | None:
        d = (device_id or "").lower()
        if not d or d == "web":
            return None
        return next((r for r in self.items() if d in r["devices"]), None)

    def save(self, d: dict) -> dict:
        rid = str(d.get("id") or secrets.token_hex(4))
        name = " ".join(str(d.get("name", "")).split())[:40]
        if len(name) < 2:
            raise ValueError("Scrivi il nome dell'aula")
        devices = sorted({str(x).strip().lower() for x in (d.get("devices") or []) if str(x).strip()})
        if any(not re.fullmatch(r"[a-z0-9:_.\-]{1,80}", x) for x in devices):
            raise ValueError("Dispositivo non valido")
        with self.store.lock:
            items = self.items()
            if any(r["name"].lower() == name.lower() and r["id"] != rid for r in items):
                raise ValueError("C'è già un'aula con questo nome")
            for r in items:  # un dispositivo sta in una sola aula
                if r["id"] != rid:
                    r["devices"] = [x for x in r["devices"] if x not in devices]
            room = next((r for r in items if r["id"] == rid), None)
            if room is None:
                room = {"id": rid}
                items.append(room)
            room.update(name=name, devices=devices)
            self.store.save()
        return room

    def delete(self, rid: str) -> bool:
        with self.store.lock:
            before = len(self.items())
            self.store.data["rooms"] = [r for r in self.items() if r["id"] != rid]
            self.store.save()
        return len(self.items()) != before

    def set_window(self, ms: int):
        ms = int(ms)
        if not 200 <= ms <= 2000:
            raise ValueError("L'attesa va da 200 a 2000 ms")
        with self.store.lock:
            self.store.data["window_ms"] = ms
            self.store.save()

    async def claim(self, device_id: str, score: float) -> bool:
        """Un dispositivo ha sentito una domanda: True se deve rispondere lui. Senza aula risponde sempre."""
        room = self.room_of(device_id)
        if room is None or len(room["devices"]) < 2:
            return True
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        contest = self._open.get(room["id"])
        fut = loop.create_future()
        if contest is None or now > contest["until"]:
            contest = self._open[room["id"]] = {"until": now + self.window, "claims": []}
            self.stats["contests"] += 1
            loop.call_later(self.window, self._decide, room["id"], contest)
        contest["claims"].append((score, device_id.lower(), fut))
        return await fut

    def _decide(self, rid: str, contest: dict):
        if self._open.get(rid) is contest:
            del self._open[rid]
        claims = contest["claims"]
        winner = max(claims, key=lambda c: c[0])
        for c in claims:
            if not c[2].done():
                c[2].set_result(c is winner)
        self.stats["silenced"] += len(claims) - 1
