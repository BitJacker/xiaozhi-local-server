"""Avvisi dal server ai dispositivi: "fine dell'attività tra 5 minuti", subito o a un orario.

Dove arrivano:
  * dispositivi xiaozhi collegati via MQTT: notifica vocale ("notify") con l'audio creato dalla voce locale e il
    testo come sottotitolo; il dispositivo lo scarica da questo server e lo riproduce anche se è a riposo;
  * conversazioni aperte (dispositivi via WebSocket, pagina dell'assistente): il testo arriva subito e, se non
    stanno già parlando, viene letto ad alta voce.
I dispositivi WebSocket a riposo non hanno una connessione aperta: per loro l'avviso arriva solo se sono in
conversazione. Gli avvisi programmati stanno in data/alerts.json.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path

from .auth import JsonStore

log = logging.getLogger("alerts")
AUDIO_TTL = 600  # i file audio delle notifiche restano disponibili 10 minuti


class Alerts:
    def __init__(self, app, data_dir: Path):
        self.app = app
        self.store = JsonStore(Path(data_dir) / "alerts.json")
        self.store.data.setdefault("scheduled", [])
        self.audio: dict[str, tuple[bytes, float]] = {}  # id -> (ogg, scadenza)

    # ------------------------------------------------------------------ invio
    async def send(self, text: str, target: str = "all", speak: bool = True) -> dict:
        text = " ".join(str(text).split())[:300]
        if not text:
            raise ValueError("Scrivi il testo dell'avviso")
        sent = {"mqtt": 0, "sessions": 0}
        gw = self.app.get("xiaozhi_gateway")
        if gw is not None:
            macs = [m for m in gw.online_devices() if target == "all" or m.lower() == target.lower()]
            if macs:
                msg = await self._notify_message(text) if speak else \
                    {"type": "alert", "status": "Avviso", "message": text, "emotion": "neutral"}
                for mac in macs:
                    sent["mqtt"] += bool(await gw.send_to_device(mac, msg))
        for s in list(self.app.get("active_sessions", set())):
            if target != "all" and s.device_id.lower() != target.lower():
                continue
            if getattr(s, "ws", None) is None or s.ws.closed or type(s.ws).__name__ == "UdpChannel":
                continue  # i dispositivi MQTT hanno già ricevuto la notifica
            await s.send_json({"session_id": s.session_id, "type": "alert", "status": "Avviso",
                               "message": text, "emotion": "neutral"})
            if speak and not s.speaking and (s.turn_task is None or s.turn_task.done()):
                asyncio.create_task(s.speak(text))
            sent["sessions"] += 1
        log.info("Avviso \"%s\" inviato a %s: %s", text[:60], target, sent)
        return sent

    async def _notify_message(self, text: str) -> dict:
        from .audio import encode_ogg_opus

        tts = self.app["tts"]
        pcm = await tts.synthesize(text)
        ogg = await asyncio.get_running_loop().run_in_executor(None, encode_ogg_opus, pcm, tts.sample_rate)
        self._cleanup()
        aid = secrets.token_urlsafe(16)  # indirizzo non indovinabile, valido 10 minuti
        self.audio[aid] = (ogg, time.time() + AUDIO_TTL)
        base = self.app.get("http_base") or ""
        return {"type": "notify", "audio_url": f"{base}/xiaozhi/notify/{aid}.ogg",
                "subtitles": [{"start_ms": 0, "text": text}]}

    def _cleanup(self):
        now = time.time()
        self.audio = {k: v for k, v in self.audio.items() if v[1] > now}

    def get_audio(self, aid: str) -> bytes | None:
        self._cleanup()
        v = self.audio.get(aid)
        return v[0] if v else None

    # ------------------------------------------------------------------ avvisi programmati
    def scheduled(self) -> list[dict]:
        return self.store.data["scheduled"]

    def save_scheduled(self, d: dict) -> dict:
        text = " ".join(str(d.get("text", "")).split())[:300]
        at = str(d.get("time", ""))
        if not text:
            raise ValueError("Scrivi il testo dell'avviso")
        try:
            h, m = (int(x) for x in at.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            raise ValueError("Orario non valido (es. 10:55)")
        days = sorted({int(x) for x in d.get("days") or range(5) if 0 <= int(x) <= 6})
        item = {"id": d.get("id") or secrets.token_hex(4), "text": text, "time": f"{h:02d}:{m:02d}", "days": days,
                "target": str(d.get("target") or "all"), "speak": bool(d.get("speak", True)),
                "enabled": bool(d.get("enabled", True)), "last": ""}
        with self.store.lock:
            items = [x for x in self.store.data["scheduled"] if x["id"] != item["id"]]
            self.store.data["scheduled"] = items + [item]
            self.store.save()
        return item

    def delete_scheduled(self, sid: str) -> bool:
        with self.store.lock:
            before = len(self.store.data["scheduled"])
            self.store.data["scheduled"] = [x for x in self.store.data["scheduled"] if x["id"] != sid]
            self.store.save()
            return len(self.store.data["scheduled"]) < before

    async def due(self, now: time.struct_time | None = None) -> list[dict]:
        """Avvisi programmati da inviare adesso (una volta sola per giorno e orario)."""
        now = now or time.localtime()
        hhmm, day = f"{now.tm_hour:02d}:{now.tm_min:02d}", time.strftime("%Y-%m-%d", now)
        out = []
        with self.store.lock:
            for x in self.store.data["scheduled"]:
                if x.get("enabled") and x["time"] == hhmm and now.tm_wday in x["days"] and x.get("last") != day:
                    x["last"] = day
                    out.append(x)
            if out:
                self.store.save()
        return out

    async def loop(self):
        while True:
            await asyncio.sleep(20)
            try:
                for x in await self.due():
                    await self.send(x["text"], x["target"], x["speak"])
                    self.app["events"].add("alert", f"programmato: {x['text'][:80]}")
            except Exception:
                log.exception("avvisi programmati")
