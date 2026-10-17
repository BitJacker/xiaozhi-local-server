"""Test delle Fasi E ed F: impostazioni (cronologia 30/60/90/illimitata, fine della frase), modelli dalla pagina Admin,
impronta vocale con consenso, un solo dispositivo per aula, musica dalla cartella del server, metriche per Prometheus
e backup cifrato. Controlla anche permessi, protezione CSRF, percorsi e cifratura sul disco.

Con il modello vero le domande sono normali; con --mock si usa "usa lo strumento ..." (LLM finto, anche finto Ollama).
Uso:  python tests/test_fase_ef.py [--mock]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from run_all import MOCK, PY, env, make_config, stop, wait_health  # noqa: E402

from xiaozhi_locale.audio import OpusDecoder, OpusEncoder, resample  # noqa: E402
from xiaozhi_locale.auth import Auth  # noqa: E402

PORT = 8102
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "tests" / "_data_ef"
BACKUPS = ROOT / "tests" / "_data_ef_backups"
H = {"X-Requested-With": "husky"}
RESULTS = []
PIPER = ROOT / "models" / "piper"
DEVICES = {"lab1": ("aa:00:00:00:00:01", "c-lab1"), "lab2": ("aa:00:00:00:00:02", "c-lab2"),
           "solo": ("aa:00:00:00:00:03", "c-solo")}
TOKENS: dict[str, str] = {}


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


async def login(user, pw):
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with s.post(BASE + "/api/login", json={"username": user, "password": pw}, headers=H) as r:
        assert r.status == 200, await r.text()
    return s


async def api(s, path, method="GET", data=None, headers=H):
    async with s.request(method, BASE + path, json=data, headers=headers) as r:
        try:
            return r.status, await r.json()
        except Exception:
            return r.status, {}


_TTS = {}


def speech(voice: str, text: str, gain: float = 1.0) -> np.ndarray:
    """Voce sintetica di una "persona" (voci Piper diverse = persone diverse), 16 kHz."""
    from xiaozhi_locale.engines import TTS
    if voice not in _TTS:
        _TTS[voice] = TTS({"voice": str(PIPER / f"{voice}.onnx")})
    t = _TTS[voice]
    pcm = resample(t._run(text), t.sample_rate, 16000).astype(np.float32) * gain
    return np.clip(pcm, -32768, 32767).astype(np.int16)


class Chat:
    """Conversazione dalla pagina dell'assistente (WebSocket del client web, audio PCM)."""

    def __init__(self, s):
        self.s, self.msgs, self.audio = s, [], 0

    async def __aenter__(self):
        self.ws = await self.s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/")
        await self.ws.send_str(json.dumps({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}}))
        self.task = asyncio.create_task(self._read())
        await self.wait(lambda m: m.get("type") == "hello")
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        await self.ws.close()

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(msg.data))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                self.audio += len(msg.data)

    async def wait(self, pred, timeout=120, start=0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for m in self.msgs[start:]:
                if pred(m):
                    return m
            await asyncio.sleep(0.05)
        return None

    async def ask(self, text: str, timeout=120) -> str:
        n = len(self.msgs)
        await self.ws.send_str(json.dumps({"type": "text", "text": text}))
        await self.wait(lambda m: m.get("type") == "tts" and m.get("state") == "stop", timeout, start=n)
        return " ".join(m.get("text", "") for m in self.msgs[n:]
                        if m.get("type") == "tts" and m.get("state") == "sentence_start")


async def device_turn(name: str, pcm: np.ndarray, delay: float = 0.0, timeout: float = 90) -> dict:
    """Un dispositivo xiaozhi (Opus 16 kHz) dice una frase; ritorna cosa ha ricevuto."""
    did, cid = DEVICES[name]
    pcm = np.concatenate([np.zeros(4800, np.int16), pcm, np.zeros(16000 * 2, np.int16)])
    packets = OpusEncoder(16000, 60).encode(pcm)
    res = {"stt": None, "sentences": [], "audio": 0, "stop": False, "closed": False}
    headers = {"Device-Id": did, "Client-Id": cid, "Authorization": f"Bearer {TOKENS[name]}", "Protocol-Version": "1"}
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/", headers=headers) as ws:
            await ws.send_json({"type": "hello", "version": 1, "transport": "websocket",
                                "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
            hello = await ws.receive_json(timeout=10)
            sid = hello["session_id"]

            async def sender():
                await asyncio.sleep(0.3 + delay)
                await ws.send_json({"session_id": sid, "type": "listen", "state": "start", "mode": "auto"})
                for p in packets:
                    await ws.send_bytes(p)
                    await asyncio.sleep(0.06)
            task = asyncio.create_task(sender())
            deadline = time.time() + timeout
            try:
                while time.time() < deadline and not res["stop"]:
                    msg = await ws.receive(timeout=max(1, deadline - time.time()))
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        res["audio"] += 1
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        m = json.loads(msg.data)
                        if m.get("type") == "stt":
                            res["stt"] = m["text"]
                        elif m.get("type") == "tts" and m.get("state") == "sentence_start":
                            res["sentences"].append(m["text"])
                        elif m.get("type") == "tts" and m.get("state") == "stop" and res["stt"]:
                            res["stop"] = True
                    else:
                        res["closed"] = True
                        break
            except asyncio.TimeoutError:
                pass
            task.cancel()
    return res


async def history_users(s) -> list[dict]:
    _, days = await api(s, "/api/history")
    if not days.get("days"):
        return []
    _, d = await api(s, f"/api/history?day={days['days'][0]}")
    return d.get("messages", [])


def make_tone_wav(path: Path, seconds: float, freq: float = 440.0):
    import wave
    t = np.arange(int(16000 * seconds)) / 16000
    pcm = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


async def main_async():
    prof = await login("prof", "Password-Prof-123")
    stud = await login("studente", "Password-Stud-123")
    ada = await login("ada", "Password-Ada-1234")
    anon = aiohttp.ClientSession()
    try:
        # ================= impostazioni: cronologia 30 / 60 / 90 / illimitata
        st, s = await api(prof, "/api/admin/settings")
        check("Impostazioni: cronologia 30 giorni e Silero di serie", st == 200 and s["history_keep_days"] == 30
              and s["vad_engine"] == "silero", str({k: s.get(k) for k in ("history_keep_days", "vad_engine")}))
        hist = DATA / "history"
        days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400)) for n in (45, 75, 120)]
        for d in days:
            (hist / f"{d}.jsonl").write_text("{}\n", encoding="utf-8")
        st, s = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 0})
        check("Cronologia illimitata: nessun giorno cancellato", st == 200 and s["history_keep_days"] == 0
              and all((hist / f"{d}.jsonl").exists() for d in days))
        st, s = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 90})
        check("Cronologia 90 giorni: cancellato solo il giorno di 120 giorni fa", st == 200
              and [(hist / f"{d}.jsonl").exists() for d in days] == [True, True, False])
        st, s = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 60})
        check("Cronologia 60 giorni: cancellato anche quello di 75 giorni fa", st == 200
              and [(hist / f"{d}.jsonl").exists() for d in days] == [True, False, False])
        st, _ = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 45})
        check("Durata non prevista (45) rifiutata", st == 400)
        st, _ = await api(stud, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 0})
        check("Uno studente non può cambiare le impostazioni", st == 403)
        st, _ = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 0},
                          headers={})
        check("Senza intestazione anti-CSRF: rifiutato", st == 403)
        st, _ = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "llm.base_url", "value": "http://x"})
        check("Impostazione non permessa dalla pagina (llm.base_url) rifiutata", st == 400)
        await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "history.keep_days", "value": 0})
        saved = json.loads((DATA / "settings.json").read_text(encoding="utf-8"))
        _, status = await api(prof, "/api/admin/status")
        check("Scelta salvata in data/settings.json e mostrata nella panoramica",
              saved.get("history.keep_days") == 0 and status["keep_days"] == 0)
        st, _ = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "vad.engine", "value": "magia"})
        st2, s = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "vad.silence_ms", "value": 700})
        check("Fine della frase: valori non validi rifiutati, silenzio cambiato", st == 400 and st2 == 200
              and s["vad_silence_ms"] == 700)

        # ================= modelli dalla pagina Admin
        st, m = await api(prof, "/api/admin/models")
        installed = [x["name"] for x in m["llm"]["installed"]]
        check("Modelli: elenco di Ollama, Whisper e voci", st == 200 and m["llm"]["ollama"]
              and m["llm"]["current"] in installed and m["whisper"]["current"]
              and any(v["default"] for v in m["voices"]["installed"]), f"{installed} whisper={m['whisper']['current']}")
        st, _ = await api(prof, "/api/admin/models", "POST", {"action": "pull", "name": "qwen3; rm -rf /"})
        st2, _ = await api(stud, "/api/admin/models", "POST", {"action": "pull", "name": "qwen3:8b"})
        check("Nome di modello pericoloso rifiutato; studente non può scaricare", st == 400 and st2 == 403)
        st, _ = await api(prof, "/api/admin/models", "POST", {"action": "delete_llm", "name": m["llm"]["current"]})
        check("Non si può eliminare il modello in uso", st == 400)
        st, _ = await api(prof, "/api/admin/models", "POST", {"action": "voice_download", "name": "../../evil"})
        check("Voce fuori dall'elenco rifiutata", st == 400)
        if MOCK:
            await api(prof, "/api/admin/models", "POST", {"action": "pull", "name": "piccolo:1b"})
            for _ in range(50):
                _, m = await api(prof, "/api/admin/models")
                if any(j["name"] == "piccolo:1b" and j["state"] != "running" for j in m["jobs"]):
                    break
                await asyncio.sleep(0.2)
            job = next(j for j in m["jobs"] if j["name"] == "piccolo:1b")
            check("Scaricamento di un modello con avanzamento", job["state"] == "done" and job["percent"] == 100, str(job))
            st, _ = await api(prof, "/api/admin/models", "POST", {"action": "use_llm", "name": "piccolo:1b"})
            async with Chat(prof) as c:
                await c.ask("Ciao")
            async with anon.get("http://127.0.0.1:1299/mock/seen") as r:
                seen = await r.json()
            check("Modello cambiato senza riavvio: la domanda dopo usa il nuovo", st == 200 and seen["model"] == "piccolo:1b",
                  seen["model"])
            await api(prof, "/api/admin/models", "POST", {"action": "use_llm", "name": "qwen3:4b-instruct-2507-q4_K_M"})
            st, _ = await api(prof, "/api/admin/models", "POST", {"action": "delete_llm", "name": "piccolo:1b"})
            check("Modello non usato eliminato", st == 200)
            await api(prof, "/api/admin/models", "POST", {"action": "pull", "name": "inesistente:1b"})
            await asyncio.sleep(1)
            _, m = await api(prof, "/api/admin/models")
            job = next(j for j in m["jobs"] if j["name"] == "inesistente:1b")
            check("Modello inesistente: errore mostrato", job["state"] == "error" and "exist" in job["status"], job["status"])
        voices = [v["name"] for v in m["voices"]["installed"]]
        if "it_IT-riccardo-x_low.onnx" in voices:
            st, _ = await api(prof, "/api/admin/models", "POST", {"action": "voice_use", "name": "it_IT-riccardo-x_low.onnx"})
            async with Chat(prof) as c:
                await c.ask("Usa lo strumento ora_attuale" if MOCK else "Che ore sono?")
                audio = c.audio
            st2, _ = await api(prof, "/api/admin/models", "POST", {"action": "voice_delete", "name": "it_IT-riccardo-x_low.onnx"})
            await api(prof, "/api/admin/models", "POST", {"action": "voice_use", "name": "it_IT-paola-medium.onnx"})
            check("Voce predefinita cambiata senza riavvio (con un'altra frequenza) e protetta dall'eliminazione",
                  st == 200 and audio > 20000 and st2 == 400, f"audio={audio} byte")
        st, _ = await api(prof, "/api/admin/models", "POST", {"action": "whisper", "name": "enorme"})
        check("Modello di Whisper non valido rifiutato", st == 400)
        st, _ = await api(prof, "/api/admin/models", "POST", {"action": "whisper", "name": "small"})
        for _ in range(240):
            _, m = await api(prof, "/api/admin/models")
            job = next((j for j in m["jobs"] if j["kind"] == "asr"), {})
            if job.get("state") != "running":
                break
            await asyncio.sleep(0.5)
        async with Chat(prof) as c:
            await c.ws.send_str(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
            pcm = speech("it_IT-paola-medium", "Quanto fa due più due?")
            for i in range(0, len(pcm), 960):
                await c.ws.send_bytes(pcm[i:i + 960].tobytes())
            await c.ws.send_str(json.dumps({"type": "listen", "state": "stop", "mode": "manual"}))
            stt = await c.wait(lambda x: x.get("type") == "stt", 60)
        check("Whisper cambiato senza riavvio e la voce si capisce ancora", job.get("state") == "done"
              and m["whisper"]["current"] == "small" and stt and "due" in stt["text"].lower(), f"{job.get('status')} {stt}")
        await api(prof, "/api/admin/models", "POST", {"action": "whisper", "name": "large-v3-turbo"})
        for _ in range(240):
            _, m = await api(prof, "/api/admin/models")
            if not any(j["kind"] == "asr" and j["state"] == "running" for j in m["jobs"]):
                break
            await asyncio.sleep(0.5)

        # ================= impronta vocale (con consenso)
        _, s = await api(prof, "/api/admin/settings")
        if not s["voiceprint"]["model"]:  # la prima volta il modello si scarica dalla pagina (26 MB)
            await api(prof, "/api/admin/settings", "POST", {"action": "voiceprint_model"})
            for _ in range(600):
                _, s = await api(prof, "/api/admin/settings")
                if s["voiceprint"]["model"] or (s["voiceprint"]["download"] or {}).get("state") == "error":
                    break
                await asyncio.sleep(0.5)
        _, me = await api(ada, "/api/me")
        st, _ = await api(ada, "/api/voiceprint", "POST", {"consent": True, "samples": []})
        check("Impronta spenta di serie: niente registrazione", not me["voiceprint"] and st == 400)
        st, s = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "voiceprint.enabled", "value": True})
        check("Impronta vocale accesa dall'admin", st == 200 and s["voiceprint"]["enabled"])

        def samples(voice):
            phrases = ["Ciao Husky, oggi in laboratorio studiamo le piante e la fotosintesi.",
                       "Mi piacciono la matematica, la musica e le passeggiate in montagna.",
                       "Leggo questa frase con calma, con la mia voce di tutti i giorni."]
            return [base64.b64encode(speech(voice, p).tobytes()).decode() for p in phrases]
        st, r = await api(ada, "/api/voiceprint", "POST", {"consent": False, "samples": samples("it_IT-paola-medium")})
        check("Senza consenso esplicito: rifiutato", st == 400 and "consenso" in r.get("error", ""))
        st, r = await api(ada, "/api/voiceprint", "POST", {"consent": "true", "samples": samples("it_IT-paola-medium")})
        check("Il consenso deve essere proprio true (non una stringa)", st == 400)
        short = [base64.b64encode(speech("it_IT-paola-medium", "Ciao.").tobytes()).decode()]
        st, r = await api(ada, "/api/voiceprint", "POST", {"consent": True, "samples": short})
        check("Registrazione troppo breve rifiutata", st == 400)
        st, r = await api(ada, "/api/voiceprint", "POST", {"consent": True, "samples": ["%%%non-base64%%%"]})
        check("Audio non valido rifiutato", st == 400)
        st, r = await api(ada, "/api/voiceprint", "POST", {"consent": True, "samples": samples("it_IT-paola-medium")})
        check("Ada registra la propria impronta (con consenso)", st == 200 and r.get("enrolled"), str(r)[:100])
        st, r = await api(prof, "/api/voiceprint", "POST", {"consent": True, "samples": samples("it_IT-riccardo-x_low")})
        check("Il prof registra la propria impronta", st == 200 and r.get("enrolled"))
        raw = (DATA / "voiceprints.json").read_text(encoding="utf-8")
        check("Impronte cifrate sul disco", "emb" not in raw and "enc1:" in raw)
        _, info = await api(stud, "/api/voiceprint")
        check("Uno studente vede solo la propria impronta (nessuna)", info.get("enrolled") is False)

        r1 = await device_turn("solo", speech("it_IT-paola-medium", "Qual è il mio colore preferito?"))
        msgs = await history_users(prof)
        mine = [x for x in msgs if x.get("device", "").lower() == DEVICES["solo"][0] and x["role"] == "user"]
        check("Dispositivo condiviso: riconosce Ada dalla voce", r1["stt"] and mine and mine[-1]["user"] == "ada",
              f"stt={r1['stt']} user={mine[-1]['user'] if mine else None}")
        if MOCK:
            async with anon.get("http://127.0.0.1:1299/mock/seen") as r:
                seen = await r.json()
            check("Il modello riceve i ricordi di Ada (e non permessi in più)", "verde" in seen["system"]
                  and "Stai parlando con ada" in seen["system"], seen["system"][-160:])
        else:
            check("Risponde con i ricordi di Ada (colore preferito: verde)", "verd" in " ".join(r1["sentences"]).lower(),
                  " ".join(r1["sentences"])[:120])
        r2 = await device_turn("solo", speech("it_IT-riccardo-x_low", "Quanto fa tre per tre?"))
        msgs = await history_users(prof)
        mine = [x for x in msgs if x.get("device", "").lower() == DEVICES["solo"][0] and x["role"] == "user"]
        check("Voce diversa: riconosce il prof, non Ada", r2["stt"] and mine[-1]["user"] == "prof", mine[-1]["user"])
        r3 = await device_turn("solo", speech("en_US-lessac-medium", "What time is it now, please?"))
        msgs = await history_users(prof)
        mine = [x for x in msgs if x.get("device", "").lower() == DEVICES["solo"][0] and x["role"] == "user"]
        check("Voce di chi non si è registrato: nessuno riconosciuto", r3["stt"] and mine[-1]["user"] == "Solo",
              mine[-1]["user"])
        _, hist_ada = await api(ada, "/api/history")
        _, d = await api(ada, f"/api/history?day={hist_ada['days'][0]}") if hist_ada.get("days") else (0, {"messages": []})
        check("Ada vede nella sua cronologia la conversazione riconosciuta (solo la sua)",
              d["messages"] and all(x["user"] == "ada" for x in d["messages"]))
        st, r = await api(ada, "/api/voiceprint", "DELETE", {})
        r4 = await device_turn("solo", speech("it_IT-paola-medium", "Quanto fa cinque più cinque?"))
        msgs = await history_users(prof)
        mine = [x for x in msgs if x.get("device", "").lower() == DEVICES["solo"][0] and x["role"] == "user"]
        check("Ada cancella l'impronta: non viene più riconosciuta", st == 200 and not r.get("enrolled")
              and mine[-1]["user"] == "Solo", mine[-1]["user"])
        await api(ada, "/api/voiceprint", "POST", {"consent": True, "samples": samples("it_IT-paola-medium")})
        await api(prof, "/api/admin/users", "DELETE", {"username": "ada"})
        raw = json.loads((DATA / "voiceprints.json").read_text(encoding="utf-8"))
        check("Account eliminato: impronta eliminata", "ada" not in raw and "prof" in raw)
        await api(prof, "/api/admin/settings", "POST", {"action": "voiceprint_clear"})
        await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "voiceprint.enabled", "value": False})

        # ================= un solo dispositivo per aula
        st, r = await api(prof, "/api/admin/rooms")
        ids = [d["id"] for d in r["devices"]]
        check("Aule: dispositivi approvati elencati", st == 200 and DEVICES["lab1"][0] in ids, str(ids))
        st, r = await api(prof, "/api/admin/rooms", "POST", {"action": "save", "name": "Laboratorio",
                                                             "devices": [DEVICES["lab1"][0], DEVICES["lab2"][0]]})
        st2, _ = await api(prof, "/api/admin/rooms", "POST", {"action": "save", "name": "x", "devices": ["../../etc"]})
        st3, _ = await api(prof, "/api/admin/rooms", "POST", {"action": "window", "window_ms": 50})
        st4, _ = await api(stud, "/api/admin/rooms", "POST", {"action": "save", "name": "Mia", "devices": []})
        check("Aula creata; dispositivo non valido, attesa fuori misura e studenti rifiutati",
              st == 200 and st2 == 400 and st3 == 400 and st4 == 403)
        q = "Quanto fa otto più uno?"
        near, far = speech("it_IT-paola-medium", q, 1.0), speech("it_IT-paola-medium", q, 0.25)
        a, b = await asyncio.gather(device_turn("lab1", far, 0.0, 60), device_turn("lab2", near, 0.1, 60))
        check("Stessa domanda a due dispositivi dell'aula: risponde solo il più vicino",
              b["stt"] and b["sentences"] and not a["stt"] and a["closed"],
              f"lab1(lontano) stt={a['stt']} closed={a['closed']} | lab2(vicino) stt={b['stt']}")
        a, b = await asyncio.gather(device_turn("lab1", near, 0.0, 60), device_turn("lab2", far, 0.1, 60))
        check("Al contrario: ora risponde l'altro", a["stt"] and not b["stt"])
        solo = await device_turn("lab1", near, 0, 60)
        check("Un solo dispositivo dell'aula che sente: risponde normalmente", solo["stt"] and solo["sentences"])
        _, r = await api(prof, "/api/admin/rooms", "POST", {"action": "save", "name": "Aula 2", "devices": [DEVICES["lab2"][0]]})
        lab = next(x for x in r["rooms"] if x["name"] == "Laboratorio")
        check("Un dispositivo sta in una sola aula", DEVICES["lab2"][0] not in lab["devices"] and r["stats"]["silenced"] >= 2,
              str(r["stats"]))

        # ================= musica dalla cartella del server
        async def upload(sess, name, data):
            f = aiohttp.FormData()
            f.add_field("file", data, filename=name)
            async with sess.post(BASE + "/api/admin/music-upload", data=f, headers=H) as r:
                return r.status, await r.json()
        wav = DATA / "campanella.wav"
        make_tone_wav(wav, 3.0)
        st, r = await upload(stud, "campanella.wav", wav.read_bytes())
        check("Uno studente non può caricare musica", st == 403)
        st, r = await upload(prof, "virus.exe", b"MZ" * 500)
        st2, r2 = await upload(prof, "finto.mp3", b"non sono un mp3 " * 100)
        check("Formato sbagliato o file non audio rifiutati", st == 400 and st2 == 400, f"{r.get('error')} / {r2.get('error')}")
        st, r = await upload(prof, "../../campanella.wav", wav.read_bytes())
        check("Nome con ../ ripulito: il file resta nella cartella della musica",
              st == 200 and r["item"]["name"] == "campanella.wav" and (DATA / "music" / "campanella.wav").exists()
              and r["item"]["seconds"] == 3, str(r))
        make_tone_wav(DATA / "lunga.wav", 30.0, 330)
        st, r = await upload(prof, "brano lungo.wav", (DATA / "lunga.wav").read_bytes())
        async with Chat(stud) as c:
            ans = await c.ask("Usa lo strumento musica_riproduci con titolo=campanella" if MOCK else "Metti la canzone campanella")
        check("Musica spenta di serie: niente brani", "♪" not in ans, ans[:80])
        st, r = await api(prof, "/api/admin/music", "POST", {"action": "enable", "enabled": True})
        check("Musica accesa dall'admin", st == 200 and r["enabled"] and len(r["items"]) == 2)
        async with Chat(stud) as c:
            t0 = time.time()
            ans = await c.ask("Usa lo strumento musica_riproduci con titolo=campanella" if MOCK else "Metti la canzone campanella")
            took = time.time() - t0
            audio = c.audio
        check("Riproduce il brano nella pagina web, al ritmo reale", "♪ campanella" in ans and audio > 3 * 22050 * 2 * 0.9
              and took >= 2.5, f"{ans[:60]} audio={audio} byte in {took:.1f} s")
        async with Chat(stud) as c:
            n = len(c.msgs)
            await c.ws.send_str(json.dumps({"type": "text", "text": "Usa lo strumento musica_riproduci con titolo=lungo"
                                            if MOCK else "Fammi sentire il brano lungo"}))
            started = await c.wait(lambda x: x.get("type") == "tts" and "♪" in x.get("text", ""), 60, n)
            await asyncio.sleep(1.5)
            t0 = time.time()
            await c.ws.send_str(json.dumps({"type": "abort"}))
            stopped = await c.wait(lambda x: x.get("type") == "tts" and x.get("state") == "stop", 5,
                                   c.msgs.index(started) if started else n)
            check("La musica si ferma subito con il pulsante (interruzione)", started and stopped and time.time() - t0 < 3)
        dev = await device_turn("solo", speech("it_IT-paola-medium", "Metti la canzone campanella"), 0, 60) if not MOCK else None
        if dev is not None:
            check("Anche su un dispositivo (Opus) parte il brano", any("♪" in s for s in dev["sentences"]) and dev["audio"] > 40,
                  f"{dev['sentences']} pacchetti={dev['audio']}")
        await api(prof, "/api/admin/music", "POST", {"action": "roles", "roles": ["admin"]})
        async with Chat(stud) as c:
            ans = await c.ask("Usa lo strumento musica_riproduci con titolo=campanella" if MOCK else "Metti la canzone campanella")
        check("Ruoli: se solo l'admin può, lo studente non la fa partire", "♪" not in ans, ans[:60])
        st, _ = await api(prof, "/api/admin/music", "POST", {"action": "roles", "roles": ["root"]})
        st2, _ = await api(prof, "/api/admin/music", "POST", {"action": "delete", "name": "../settings.json"})
        check("Ruolo inventato e cancellazione fuori cartella rifiutati", st == 400 and st2 == 400
              and (DATA / "settings.json").exists())

        # ================= metriche per Prometheus
        async with anon.get(BASE + "/metrics") as r:
            off = r.status
        st, s = await api(prof, "/api/admin/settings", "POST", {"action": "set", "key": "metrics.enabled", "value": True})
        async with anon.get(BASE + "/metrics") as r:
            no_tok = r.status
        st2, _ = await api(stud, "/api/admin/settings", "POST", {"action": "metrics_token"})
        _, s = await api(prof, "/api/admin/settings", "POST", {"action": "metrics_token"})
        token = s.get("token", "")
        async with anon.get(BASE + "/metrics", headers={"Authorization": "Bearer sbagliato"}) as r:
            bad = r.status
        async with anon.get(BASE + "/metrics", headers={"Authorization": f"Bearer {token}"}) as r:
            ok, text = r.status, await r.text()
        check("Metriche: spente=404, senza token=401, token sbagliato=401, studente non crea token",
              off == 404 and no_tok == 401 and bad == 401 and st2 == 403)
        check("Metriche con il token: domande, tempi, connessioni", ok == 200 and "husky_turns_total" in text
              and "husky_first_audio_seconds_bucket" in text and "husky_room_silenced_total" in text, text[:80])
        check("Metriche senza nomi né testo delle conversazioni",
              not any(w in text for w in ("prof", "studente", "campanella", "colore", "Laboratorio")))
        stored = (DATA / "metrics.json").read_text(encoding="utf-8")
        check("Token delle metriche salvato solo come hash", token and token not in stored)

        # ================= backup cifrato
        st, _ = await api(prof, "/api/admin/backup", "POST", {"action": "save", "enabled": True})
        st2, _ = await api(prof, "/api/admin/backup", "POST", {"action": "password", "password": "corta"})
        check("Backup: non si accende senza password; password corta rifiutata", st == 400 and st2 == 400)
        st, _ = await api(prof, "/api/admin/backup", "POST", {"action": "save", "folder": str(DATA / "dentro")})
        check("Cartella dei backup dentro i dati rifiutata", st == 400)
        pw = "Password-Backup-2026"
        await api(prof, "/api/admin/backup", "POST", {"action": "password", "password": pw})
        st, r = await api(prof, "/api/admin/backup", "POST", {"action": "save", "enabled": True, "time": "03:15",
                                                              "keep": 2, "folder": str(BACKUPS)})
        check("Backup automatico acceso (ora, copie, cartella)", st == 200 and r["enabled"] and r["time"] == "03:15")
        names = []
        for _ in range(3):
            st, r = await api(prof, "/api/admin/backup", "POST", {"action": "run"})
            names.append(r.get("created", {}).get("name"))
            await asyncio.sleep(1.1)
        check("Backup a mano: tiene solo le ultime 2 copie", st == 200 and len(r["files"]) == 2
              and r["files"][0]["name"] == names[-1], str([f["name"] for f in r["files"]]))
        name = r["files"][0]["name"]
        async with prof.get(BASE + f"/api/admin/backup-file?name={name}") as resp:
            blob = await resp.read()
        async with stud.get(BASE + f"/api/admin/backup-file?name={name}") as resp:
            st_stud = resp.status
        async with prof.get(BASE + "/api/admin/backup-file?name=../settings.json") as resp:
            st_trav = resp.status
        check("Scaricamento: solo admin, niente percorsi fuori cartella", blob[:8] == b"HUSKYBK1" and st_stud == 403
              and st_trav == 404)
        check("Il backup è cifrato (niente nomi di file o utenti in chiaro)",
              b"users.json" not in blob and b"prof" not in blob and b"settings" not in blob)
        f = BACKUPS / name
        e = {**env, "BKPW": pw, "BKBAD": "sbagliata-123"}
        good = subprocess.run([PY, "-m", "xiaozhi_locale.backup", "verify", str(f), "--password-env", "BKPW"], cwd=ROOT,
                              env=e, capture_output=True, text=True)
        badp = subprocess.run([PY, "-m", "xiaozhi_locale.backup", "verify", str(f), "--password-env", "BKBAD"], cwd=ROOT,
                              env=e, capture_output=True, text=True)
        check("Verifica con la password giusta; con quella sbagliata fallisce", good.returncode == 0 and badp.returncode != 0,
              (good.stdout + good.stderr).strip()[-80:])
        target = BACKUPS / "ripristino" / "data"
        rest = subprocess.run([PY, "-m", "xiaozhi_locale.backup", "restore", str(f), "--data", str(target),
                               "--password-env", "BKPW"], cwd=ROOT, env=e, capture_output=True, text=True)
        same = (target / "users.json").exists() and json.loads((target / "users.json").read_text(encoding="utf-8")).keys() \
            == json.loads((DATA / "users.json").read_text(encoding="utf-8")).keys()
        check("Ripristino in una cartella: utenti e impostazioni tornano", rest.returncode == 0 and same
              and (target / "settings.json").exists() and not (target / "music").exists(), rest.stdout.strip()[-80:])
        trunc = BACKUPS / "troncato.husky-backup"
        trunc.write_bytes(f.read_bytes()[:-100])
        tr = subprocess.run([PY, "-m", "xiaozhi_locale.backup", "verify", str(trunc), "--password-env", "BKPW"], cwd=ROOT,
                            env=e, capture_output=True, text=True)
        check("Backup troncato riconosciuto (non si ripristina a metà)", tr.returncode != 0)

        # ================= registro
        _, ev = await api(prof, "/api/admin/events")
        kinds = {x["kind"] for x in ev["events"]}
        check("Registro: impostazioni, modelli, impronta, aule, musica, backup",
              {"settings", "models", "voiceprint", "rooms", "music", "backup"} <= kinds, str(kinds))
        for page in ("/admin", "/account"):
            async with prof.get(BASE + page) as r:
                html = await r.text()
            check(f"Pagina {page} con le nuove sezioni", r.status == 200 and ("backup" in html if page == "/admin" else "vp-box" in html))
    finally:
        for s in (prof, stud, ada, anon):
            await s.close()


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(BACKUPS, ignore_errors=True)
    DATA.mkdir(parents=True)
    (DATA / "history").mkdir()
    a = Auth({"enabled": True}, DATA)
    a.create_user("prof", "Password-Prof-123", "admin")
    a.create_user("studente", "Password-Stud-123", "user")
    a.create_user("ada", "Password-Ada-1234", "user")
    for name, (did, cid) in DEVICES.items():
        st = a.device_status(did, cid, "127.0.0.1")
        a.approve_device(code=st["code"], name=name.capitalize())
        TOKENS[name] = a.device_token(did, cid)
    from xiaozhi_locale.cryptobox import Box
    from xiaozhi_locale.memory import Memory
    Memory(DATA, Box(DATA / "encryption.key")).add("ada", "Il suo colore preferito è il verde")
    cfg = make_config(PORT, "tests/_data_ef", auth=True, turns=500, huskylens=False)
    (ROOT / "tests" / "config.ef.yaml").write_text(cfg, encoding="utf-8")
    procs = []
    if MOCK and not _port_open(1299):
        procs.append(subprocess.Popen([PY, "tests/mock_llm.py"], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    log = open(DATA / "server.log", "w", encoding="utf-8")
    procs.append(subprocess.Popen([PY, "server.py", "-c", "tests/config.ef.yaml"], cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT))
    try:
        if not wait_health(BASE + "/health", '"ok"'):
            print("server non partito:\n" + (DATA / "server.log").read_text(encoding="utf-8")[-2000:])
            return 1
        asyncio.run(main_async())
    finally:
        stop(procs)
        log.close()
    if all(RESULTS):
        shutil.rmtree(DATA, ignore_errors=True)
        shutil.rmtree(BACKUPS, ignore_errors=True)
    (ROOT / "tests" / "config.ef.yaml").unlink(missing_ok=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test delle fasi E ed F superati ====")
    return 0 if all(RESULTS) else 1


def _port_open(port: int) -> bool:
    with socket.socket() as so:
        so.settimeout(0.3)
        return so.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
