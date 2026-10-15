"""Suite di test end-to-end del server locale.

Prerequisiti (li avvia tests/run_all.py):
  * LM Studio con il modello di config.yaml
  * tests/fake_huskylens.py su http://127.0.0.1:3000/sse
  * server.py -c tests/config.suite.yaml  (porta 8079, token "test123")

Uso:  .venv\\Scripts\\python tests\\test_suite.py [http://localhost:8079]
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale.audio import OpusDecoder, OpusEncoder, resample  # noqa: E402
from xiaozhi_locale.engines import TTS  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8079"
TOKEN = "test123"
_tts = TTS({"voice": str(ROOT / "models/piper/it_IT-paola-medium.onnx")})
_speech_cache: dict[str, np.ndarray] = {}


def speech(text: str, tail_silence_s: float = 1.5) -> np.ndarray:
    if text not in _speech_cache:
        _speech_cache[text] = resample(_tts._run(text), _tts.sample_rate, 16000)
    return np.concatenate([np.zeros(4800, np.int16), _speech_cache[text],
                           np.zeros(int(16000 * tail_silence_s), np.int16)])


class Device:
    """Dispositivo xiaozhi simulato (Opus) o client web (PCM)."""

    def __init__(self, http, version=1, pcm=False, device_id="aa:bb:cc:00:00:01", token=TOKEN, mcp=True):
        self.http, self.version, self.pcm, self.device_id = http, version, pcm, device_id
        self.token, self.mcp = token, mcp
        self.events: list[dict] = []
        self.audio_packets = 0
        self.audio_samples = 0
        self.tool_calls: list[dict] = []
        self.bad_headers = 0
        self._dec = OpusDecoder(24000)
        self._new = asyncio.Event()

    async def connect(self):
        ws_url = BASE.replace("http", "ws") + "/xiaozhi/v1/"
        headers = {"Device-Id": self.device_id, "Client-Id": "suite", "Protocol-Version": str(self.version)}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.ws = await self.http.ws_connect(ws_url, headers=headers)
        ap = ({"format": "pcm", "sample_rate": 16000, "channels": 1} if self.pcm else
              {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60})
        hello = {"type": "hello", "version": self.version, "transport": "websocket", "audio_params": ap}
        if self.mcp:
            hello["features"] = {"mcp": True}
        await self.ws.send_str(json.dumps(hello))
        self.hello = json.loads((await self.ws.receive(timeout=10)).data)
        self.sid = self.hello["session_id"]
        self._reader = asyncio.create_task(self._read())
        return self

    def _unwrap(self, data: bytes) -> bytes:
        if self.version == 2:
            ver, typ, _, _, size = struct.unpack(">HHIII", data[:16])
            if ver != 2 or typ != 0 or size != len(data) - 16:
                self.bad_headers += 1
            return data[16:]
        if self.version == 3:
            typ, _, size = struct.unpack(">BBH", data[:4])
            if typ != 0 or size != len(data) - 4:
                self.bad_headers += 1
            return data[4:]
        return data

    def _wrap(self, payload: bytes) -> bytes:
        if self.version == 2:
            return struct.pack(">HHIII", 2, 0, 0, 0, len(payload)) + payload
        if self.version == 3:
            return struct.pack(">BBH", 0, 0, len(payload)) + payload
        return payload

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                self.audio_packets += 1
                payload = self._unwrap(msg.data)
                if self.pcm:
                    self.audio_samples += len(payload) // 2
                else:
                    self.audio_samples += len(self._dec.decode(payload))
                continue
            m = json.loads(msg.data)
            if m["type"] == "mcp":
                await self._mcp(m["payload"])
                continue
            m["_t"] = time.time()
            self.events.append(m)
            self._new.set()

    async def _mcp(self, p):
        if p.get("method") == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
        elif p.get("method") == "tools/list":
            # due pagine, per verificare la paginazione con nextCursor
            if not p["params"].get("cursor"):
                res = {"tools": [{"name": "self.audio_speaker.set_volume", "description": "Imposta il volume dell'altoparlante (0-100)",
                                  "inputSchema": {"type": "object", "properties": {"volume": {"type": "integer"}}, "required": ["volume"]}}],
                       "nextCursor": "p2"}
            else:
                res = {"tools": [{"name": "self.screen.set_brightness", "description": "Imposta la luminosità dello schermo (0-100)",
                                  "inputSchema": {"type": "object", "properties": {"brightness": {"type": "integer"}}, "required": ["brightness"]}}],
                       "nextCursor": ""}
        elif p.get("method") == "tools/call":
            self.tool_calls.append(p["params"])
            res = {"content": [{"type": "text", "text": "true"}], "isError": False}
        else:
            return
        await self.ws.send_str(json.dumps({"session_id": self.sid, "type": "mcp",
                                           "payload": {"jsonrpc": "2.0", "id": p["id"], "result": res}}))

    async def send(self, obj: dict):
        obj.setdefault("session_id", self.sid)
        await self.ws.send_str(json.dumps(obj))

    async def stream(self, pcm: np.ndarray, realtime=True):
        if self.pcm:
            for i in range(0, len(pcm), 960):
                await self.ws.send_bytes(self._wrap(pcm[i:i + 960].tobytes()))
                if realtime:
                    await asyncio.sleep(0.06)
            return
        for p in OpusEncoder(16000, 60).encode(pcm):
            await self.ws.send_bytes(self._wrap(p))
            if realtime:
                await asyncio.sleep(0.06)

    async def wait_for(self, pred, timeout=60) -> dict | None:
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            for e in self.events[seen:]:
                if pred(e):
                    return e
            seen = len(self.events)
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), timeout=max(0.05, deadline - time.time()))
            except asyncio.TimeoutError:
                pass
        return None

    def texts(self, state="sentence_start") -> str:
        return " ".join(e.get("text", "") for e in self.events if e["type"] == "tts" and e.get("state") == state)

    def stts(self) -> list[str]:
        return [e["text"] for e in self.events if e["type"] == "stt"]

    async def close(self):
        await self.ws.close()
        self._reader.cancel()


is_stop = lambda e: e["type"] == "tts" and e.get("state") == "stop"  # noqa: E731


async def ask_voice(dev: Device, text: str, mode="auto", timeout=90) -> dict:
    n0 = len(dev.events)
    await dev.send({"type": "listen", "state": "start", "mode": mode})
    await dev.stream(speech(text))
    if mode == "manual":
        await dev.send({"type": "listen", "state": "stop"})
    stop = await dev.wait_for(lambda e: is_stop(e) and dev.events.index(e) >= n0, timeout)
    new = dev.events[n0:]
    return {"stop": stop, "stt": [e["text"] for e in new if e["type"] == "stt"],
            "answer": " ".join(e["text"] for e in new if e["type"] == "tts" and e.get("state") == "sentence_start")}


# ======================================================================= test
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


async def t_ota(http):
    hdr = {"Device-Id": "aa:bb:cc:00:00:09", "Client-Id": "x"}
    async with http.post(f"{BASE}/xiaozhi/ota/", json={"application": {"version": "2.0.3"}}, headers=hdr) as r:
        j = await r.json()
    ok = (r.status == 200 and j["websocket"]["url"].endswith("/xiaozhi/v1/") and j["websocket"]["token"] == TOKEN
          and j["firmware"]["version"] == "2.0.3" and j["firmware"]["url"] == "" and "activation" not in j
          and abs(j["server_time"]["timestamp"] / 1000 - time.time()) < 5)
    check("OTA POST: websocket+token, nessun aggiornamento, nessuna attivazione", ok, j["websocket"]["url"])
    async with http.get(f"{BASE}/xiaozhi/ota/") as r:
        check("OTA GET senza corpo", r.status == 200 and "websocket" in await r.json())
    async with http.post(f"{BASE}/xiaozhi/ota/activate", json={}) as r:
        check("OTA activate risponde 200", r.status == 200)


async def t_auth(http):
    try:
        d = await Device(http, token="sbagliato").connect()
        await d.close()
        check("Token errato rifiutato", False)
    except aiohttp.WSServerHandshakeError as e:
        check("Token errato rifiutato", e.status == 401, f"HTTP {e.status}")
    try:
        d = await Device(http, token=None).connect()
        await d.close()
        check("Senza token rifiutato", False)
    except aiohttp.WSServerHandshakeError as e:
        check("Senza token rifiutato", e.status == 401, f"HTTP {e.status}")


async def t_hello_and_mcp(http):
    d = await Device(http).connect()
    ap = d.hello["audio_params"]
    check("hello: transport websocket, opus 24 kHz 60 ms",
          d.hello["transport"] == "websocket" and ap["format"] == "opus" and ap["sample_rate"] == 24000 and ap["frame_duration"] == 60)
    async with http.get(f"{BASE}/health") as r:
        h = await r.json()
    check("HuskyLens (MCP SSE) collegato", h["mcp_esterni"].get("huskylens", 0) >= 2, str(h["mcp_esterni"]))
    await d.close()


async def t_voice_tool(http, version):
    d = await Device(http, version=version, device_id=f"aa:bb:cc:00:00:2{version}").connect()
    await asyncio.sleep(1)
    r = await ask_voice(d, "Imposta il volume a sessanta.")
    call = d.tool_calls[0] if d.tool_calls else {}
    ok = (r["stop"] is not None and call.get("name") == "self.audio_speaker.set_volume"
          and int(call.get("arguments", {}).get("volume", -1)) == 60 and d.audio_samples > 16000 and d.bad_headers == 0)
    check(f"Voce -> tool dispositivo (protocollo v{version}, tool in 2 pagine)", ok,
          f"stt={r['stt']} tool={call} audio={d.audio_samples / 24000:.1f}s risposta='{r['answer']}'")
    await d.close()


async def t_manual(http):
    d = await Device(http, device_id="aa:bb:cc:00:00:31").connect()
    r = await ask_voice(d, "Quanto fa sette per otto?", mode="manual")
    check("Modalità manuale (tieni premuto)", r["stop"] is not None and "56" in r["answer"].replace("cinquantasei", "56"),
          f"stt={r['stt']} risposta='{r['answer']}'")
    # dopo la risposta in manuale il server NON deve riascoltare da solo
    n = len(d.events)
    await d.stream(speech("Questa frase non va ascoltata."), realtime=False)
    await asyncio.sleep(4)
    check("Manuale: audio fuori da listen ignorato", not any(e["type"] == "stt" for e in d.events[n:]))
    await d.close()


async def t_memory(http):
    d = await Device(http, device_id="aa:bb:cc:00:00:41").connect()
    first = await ask_voice(d, "Ciao, mi chiamo Marco.")
    # il nome come l'ha capito Whisper (la voce sintetica di test non è sempre perfetta)
    heard = first["stt"][0].lower().split("chiamo")[-1].strip(" .,!?") if first["stt"] else "marco"
    r = await ask_voice(d, "Come mi chiamo?")
    check("Memoria della conversazione (stessa sessione)", heard and heard in r["answer"].lower(),
          f"nome capito='{heard}' risposta='{r['answer']}'")
    await d.close()
    d2 = await Device(http, device_id="aa:bb:cc:00:00:41").connect()
    await d2.send({"type": "text", "text": "Come mi chiamo? Se non lo sai dillo."})
    await d2.wait_for(is_stop, 60)
    check("Nuova connessione: memoria cancellata", "marco" not in d2.texts().lower(), f"risposta='{d2.texts()}'")
    await d2.close()


async def t_huskylens(http):
    d = await Device(http, device_id="aa:bb:cc:00:00:51").connect()
    r = await ask_voice(d, "Cosa vedi con la telecamera?")
    check("Domanda visiva -> tool HuskyLens", "alex" in r["answer"].lower(), f"stt={r['stt']} risposta='{r['answer']}'")
    await d.close()


async def t_abort(http):
    d = await Device(http, device_id="aa:bb:cc:00:00:61").connect()
    await d.send({"type": "text", "text": "Raccontami una storia di almeno dieci frasi su un cane che va al parco."})
    start = await d.wait_for(lambda e: e["type"] == "tts" and e.get("state") == "start", 60)
    await asyncio.sleep(1.5)
    t0 = time.time()
    await d.send({"type": "abort", "reason": "wake_word_detected"})
    stop = await d.wait_for(is_stop, 10)
    check("Interruzione (abort) ferma la voce", start is not None and stop is not None and stop["_t"] - t0 < 1.5,
          f"stop dopo {stop['_t'] - t0:.2f}s" if stop else "nessuno stop")
    await d.close()


async def t_interrupt_web(http):
    """Client web: mentre parla, premo per parlare e faccio un'altra domanda."""
    d = await Device(http, pcm=True, mcp=False, device_id="web-int").connect()
    await d.send({"type": "text", "text": "Raccontami una storia lunga almeno dieci frasi su un drago."})
    await d.wait_for(lambda e: e["type"] == "tts" and e.get("state") == "start", 60)
    await asyncio.sleep(2)
    pkts_before = d.audio_packets
    t0 = time.time()
    n0 = len(d.events)
    await d.send({"type": "abort"})
    await d.send({"type": "listen", "state": "start", "mode": "manual"})
    stop = await d.wait_for(lambda e: is_stop(e) and d.events.index(e) >= n0, 5)
    await asyncio.sleep(0.5)
    pkts_after_abort = d.audio_packets - pkts_before
    await d.stream(speech("Quanto fa cinque più cinque?"))
    await d.send({"type": "listen", "state": "stop"})
    stt = await d.wait_for(lambda e: e["type"] == "stt" and d.events.index(e) >= n0, 30)
    await d.wait_for(lambda e: is_stop(e) and d.events.index(e) > d.events.index(stt), 60) if stt else None
    ok = (stop is not None and stop["_t"] - t0 < 1.0 and pkts_after_abort <= 12 and stt is not None
          and ("5" in stt["text"] or "cinque" in stt["text"].lower()) and ("10" in d.texts() or "dieci" in d.texts().lower()))
    check("Interruzione con nuova domanda (client web)", ok,
          f"stop dopo {(stop['_t'] - t0) if stop else -1:.2f}s, pacchetti dopo abort={pkts_after_abort}, "
          f"nuova domanda={stt['text'] if stt else None}")
    await d.close()


async def t_silence_and_wake(http):
    d = await Device(http, device_id="aa:bb:cc:00:00:71").connect()
    await d.send({"type": "listen", "state": "detect", "text": "Hi Husky"})
    await d.send({"type": "listen", "state": "start", "mode": "auto"})
    noise = (np.random.default_rng(1).standard_normal(16000 * 3) * 60).astype(np.int16)  # rumore di fondo leggero
    await d.stream(noise)
    await asyncio.sleep(3)
    check("Parola di attivazione + solo rumore: nessuna risposta", not d.stts(), str(d.stts()))
    await d.close()


async def t_web_pcm(http):
    d = await Device(http, pcm=True, mcp=False, device_id="web").connect()
    check("Client web: hello PCM", d.hello["audio_params"]["format"] == "pcm")
    r = await ask_voice(d, "Qual è la capitale della Francia?", mode="manual")
    check("Client web: voce PCM -> risposta + audio", "parigi" in r["answer"].lower() and d.audio_samples > 10000,
          f"stt={r['stt']} risposta='{r['answer']}'")
    await d.close()


async def t_vision(http):
    form = aiohttp.FormData()
    form.add_field("question", "Cosa c'è?")
    form.add_field("file", b"\xff\xd8\xff\xe0fakejpeg", filename="camera.jpg", content_type="image/jpeg")
    async with http.post(f"{BASE}/xiaozhi/vision/explain", data=form, headers={"Authorization": f"Bearer {TOKEN}"}) as r:
        j = await r.json()
    check("Endpoint visione risponde (senza modello visivo)", r.status == 200 and j["success"], j["result"])
    async with http.post(f"{BASE}/xiaozhi/vision/explain", data=aiohttp.FormData({"question": "x"})) as r:
        check("Visione senza token rifiutata", r.status == 401)


async def t_concurrency(http):
    devs = [await Device(http, device_id=f"aa:bb:cc:00:01:0{i}").connect() for i in range(3)]
    qs = ["Quanto fa due più due?", "Di che colore è il cielo?", "Quante zampe ha un gatto?"]
    t0 = time.time()
    rs = await asyncio.gather(*(ask_voice(d, q, timeout=120) for d, q in zip(devs, qs)))
    ok = all(r["stop"] is not None and r["answer"] for r in rs)
    check("3 dispositivi in parallelo", ok, f"{time.time() - t0:.1f}s totali | " + " | ".join(r["answer"][:40] for r in rs))
    for d in devs:
        await d.close()


async def t_latency(http):
    d = await Device(http, device_id="aa:bb:cc:00:00:81").connect()
    await d.send({"type": "listen", "state": "start", "mode": "auto"})
    await d.stream(speech("Dimmi un colore.", tail_silence_s=0.0))
    t_end = time.time()
    await d.stream(np.zeros(16000 * 2, np.int16))  # silenzio che fa scattare il VAD
    first_audio = None
    while time.time() - t_end < 30:
        if d.audio_packets:
            first_audio = time.time()
            break
        await asyncio.sleep(0.02)
    lat = (first_audio - t_end) if first_audio else None
    check("Latenza fine frase -> primo audio < 4 s", lat is not None and lat < 4, f"{lat:.2f}s" if lat else "nessun audio")
    await d.wait_for(is_stop, 30)
    await d.close()


async def main():
    async with aiohttp.ClientSession() as http:
        for t in (t_ota, t_auth, t_hello_and_mcp):
            await t(http)
        for v in (1, 2, 3):
            await t_voice_tool(http, v)
        for t in (t_manual, t_memory, t_huskylens, t_abort, t_interrupt_web, t_silence_and_wake, t_web_pcm, t_vision, t_latency, t_concurrency):
            try:
                await t(http)
            except Exception as e:
                check(t.__name__, False, f"eccezione: {e!r}")
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n==== {passed}/{len(RESULTS)} test superati ====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
