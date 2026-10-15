"""Gestione di una connessione WebSocket secondo il protocollo xiaozhi.

Flusso: hello -> listen(start) -> audio Opus -> [fine frase] -> stt -> llm -> tts(start,
sentence_start, audio..., stop). Vedi docs/websocket.md del progetto xiaozhi-esp32.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import uuid

import numpy as np
from aiohttp import WSMsgType, web

from .audio import EnergyVAD, OpusDecoder, OpusEncoder, resample
from .engines import clean_text, split_sentences
from .tools import DeviceMCP, ToolSet

log = logging.getLogger("session")

DOWN_RATE = 24000   # audio verso i dispositivi Opus
FRAME_MS = 60


class Session:
    def __init__(self, app: web.Application, ws: web.WebSocketResponse, request: web.Request):
        self.app = app
        self.cfg = app["cfg"]
        self.ws = ws
        self.req = request
        self.session_id = str(uuid.uuid4())
        self.device_id = request.headers.get("Device-Id") or request.query.get("device-id") or "web"
        self.protocol_version = int(request.headers.get("Protocol-Version", "1") or 1)

        self.pcm_mode = False          # True per il client web (PCM grezzo, niente Opus)
        self.decoder: OpusDecoder | None = None
        self.listen_mode = "auto"
        self.listening = False
        self.speaking = False
        self.abort = asyncio.Event()
        self.buffer: list[np.ndarray] = []
        vcfg = self.cfg.get("vad", {})
        self.vad = EnergyVAD(vcfg.get("silence_ms", 800), vcfg.get("min_speech_ms", 300),
                             vcfg.get("energy_threshold", 0.012), vcfg.get("max_utterance_s", 20))
        self.history: list[dict] = []
        self.device_mcp = DeviceMCP(self.send_json, self.session_id)
        self.turn_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- invio
    async def send_json(self, obj: dict):
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def send_audio(self, payload: bytes):
        if self.ws.closed:
            return
        if self.protocol_version == 2:
            payload = struct.pack(">HHIII", 2, 0, 0, 0, len(payload)) + payload
        elif self.protocol_version == 3:
            payload = struct.pack(">BBH", 0, 0, len(payload)) + payload
        await self.ws.send_bytes(payload)

    def _unwrap(self, data: bytes) -> tuple[int, bytes]:
        """Ritorna (tipo, payload). tipo 0 = audio, 1 = JSON."""
        if self.protocol_version == 2 and len(data) >= 16:
            _, typ, _, _, size = struct.unpack(">HHIII", data[:16])
            return typ, data[16:16 + size]
        if self.protocol_version == 3 and len(data) >= 4:
            typ, _, size = struct.unpack(">BBH", data[:4])
            return typ, data[4:4 + size]
        return 0, data

    # ---------------------------------------------------------------- ciclo principale
    async def run(self):
        log.info("Connessione da %s (device %s, protocollo v%d)", self.req.remote, self.device_id, self.protocol_version)
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self.on_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    typ, payload = self._unwrap(msg.data)
                    if typ == 1:
                        await self.on_text(payload.decode("utf-8", "replace"))
                    else:
                        self.on_audio(payload)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if self.turn_task:
                self.turn_task.cancel()
            self.history.clear()  # nessuna conversazione resta in memoria
            log.info("Disconnesso %s", self.device_id)

    async def on_text(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = msg.get("type")
        if t == "hello":
            await self.on_hello(msg)
        elif t == "listen":
            state = msg.get("state")
            if state == "start":
                self.listen_mode = msg.get("mode", self.listen_mode)
                if self.listen_mode == "manual":
                    await self.interrupt()  # premere per parlare interrompe la risposta in corso
                self.start_listening()
            elif state == "stop":
                self.listening = False
                if self.listen_mode == "manual":
                    self.finish_utterance()
            elif state == "detect":
                log.info("Parola di attivazione: %s", msg.get("text"))
                self.start_listening()
        elif t == "abort":
            await self.interrupt()
        elif t == "mcp":
            self.device_mcp.handle(msg.get("payload") or {})
        elif t == "text":  # estensione per il client web: domanda scritta
            self.launch_turn(msg.get("text", ""))

    async def on_hello(self, msg: dict):
        ap = msg.get("audio_params") or {}
        self.pcm_mode = ap.get("format") == "pcm"
        if self.pcm_mode:
            down = {"format": "pcm", "sample_rate": self.app["tts"].sample_rate, "channels": 1}
        else:
            self.decoder = OpusDecoder(int(ap.get("sample_rate", 16000)))
            down = {"format": "opus", "sample_rate": DOWN_RATE, "channels": 1, "frame_duration": FRAME_MS}
        await self.send_json({"type": "hello", "transport": "websocket",
                              "session_id": self.session_id, "audio_params": down})
        if (msg.get("features") or {}).get("mcp"):
            asyncio.create_task(self._init_device_mcp())

    async def _init_device_mcp(self):
        try:
            token = self.app["token_for_devices"]
            await self.device_mcp.initialize(self.app["vision_url"], token)
        except Exception as e:
            log.warning("MCP dispositivo non disponibile: %s", e)

    # ---------------------------------------------------------------- ascolto
    def start_listening(self):
        self.listening = True
        self.buffer.clear()
        self.vad.reset()

    def on_audio(self, payload: bytes):
        if not self.listening or self.speaking:
            return  # evita di riascoltare la propria voce
        if self.pcm_mode:
            pcm = np.frombuffer(payload[: len(payload) // 2 * 2], dtype=np.int16)
        else:
            if self.decoder is None:
                self.decoder = OpusDecoder(16000)
            pcm = self.decoder.decode(payload)
        self.buffer.append(pcm)
        if self.listen_mode != "manual" and self.vad.feed(pcm):
            self.listening = False
            self.finish_utterance()

    def finish_utterance(self):
        if not self.buffer:
            return
        pcm = np.concatenate(self.buffer)
        self.buffer.clear()
        self.vad.reset()
        if len(pcm) < 16000 * 0.3:
            self.listening = self.listen_mode != "manual"
            return
        self.launch_turn(pcm)

    async def interrupt(self):
        """Ferma subito la risposta in corso (voce, LLM, tool) e torna ad ascoltare."""
        task = self.turn_task
        if task and not task.done():
            self.abort.set()
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except BaseException:
                pass
            log.info("[%s] Risposta interrotta", self.device_id)
        self.speaking = False

    def launch_turn(self, item):
        if self.turn_task and not self.turn_task.done():
            self.abort.set()
            self.turn_task.cancel()
        self.turn_task = asyncio.create_task(self.turn(item))

    # ---------------------------------------------------------------- turno di dialogo
    async def turn(self, item):
        self.abort.clear()
        cfg = self.cfg
        try:
            if isinstance(item, str):
                text = item.strip()
            else:
                t0 = time.time()
                text = await self.app["asr"].transcribe(item)
                log.debug("ASR %.1fs", time.time() - t0)
            if not text:
                await self.resume_listening()
                return
            if cfg["server"].get("log_transcripts"):
                log.info("[%s] Utente: %s", self.device_id, text)
            await self.send_json({"session_id": self.session_id, "type": "stt", "text": text})
            await self.send_json({"session_id": self.session_id, "type": "llm", "emotion": "thinking", "text": "🤔"})

            tools = ToolSet()
            tools.merge(self.device_mcp.tools)
            for ext in self.app["external_mcp"]:
                tools.merge(ext.tools)
            camera = any(len(ext.tools) for ext in self.app["external_mcp"])
            # il prompt di sistema dice al modello se la telecamera è collegata in questo momento
            system = cfg["assistant"]["system_prompt"].strip() + "\n" + (
                "La telecamera HuskyLens è collegata: usa i suoi strumenti quando l'utente chiede cosa vedi, "
                "di cambiare modello, di imparare o riconoscere qualcosa."
                if camera else
                "La telecamera HuskyLens NON è collegata in questo momento: se l'utente chiede di vedere o "
                "riconoscere qualcosa, spiega che la telecamera non è collegata. Non parlare di HuskyLens se non te lo chiedono.")
            now = time.localtime()
            giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
            system += (f"\nOggi è {giorni[now.tm_wday]} {now.tm_mday:02d}/{now.tm_mon:02d}/{now.tm_year}, "
                       f"sono le {now.tm_hour}:{now.tm_min:02d}.")
            if not self.history:
                self.history.append({"role": "system", "content": system})
            else:
                self.history[0] = {"role": "system", "content": system}
            self.history.append({"role": "user", "content": text})
            try:
                answer = await self.app["llm"].chat(self.history, tools)
            except Exception as e:
                log.error("LLM non raggiungibile: %s", e)
                answer = "Il modello linguistico locale non risponde. Controlla che il server LLM sia avviato."
            answer = clean_text(answer)
            self._trim_history()
            if cfg["server"].get("log_transcripts"):
                log.info("[%s] Assistente: %s", self.device_id, answer)
            await self.send_json({"session_id": self.session_id, "type": "llm", "emotion": "happy", "text": "🙂"})
            await self.speak(answer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Errore nel turno")
        await self.resume_listening()

    def _trim_history(self):
        max_msgs = 2 * int(self.cfg["assistant"].get("max_history_turns", 10))
        if len(self.history) > max_msgs + 1:
            rest = self.history[-max_msgs:]
            while rest and rest[0]["role"] != "user":  # non spezzare sequenze tool
                rest.pop(0)
            self.history = [self.history[0]] + rest

    async def resume_listening(self):
        if self.listen_mode != "manual":
            self.start_listening()

    async def speak(self, text: str):
        tts = self.app["tts"]
        self.speaking = True
        await self.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
        try:
            encoder = None if self.pcm_mode else OpusEncoder(DOWN_RATE, FRAME_MS)
            sentences = split_sentences(text) or ["..."]
            # la sintesi della frase successiva parte mentre si invia quella corrente
            jobs = [asyncio.ensure_future(tts.synthesize(sentences[0]))]
            for idx, sentence in enumerate(sentences):
                if self.abort.is_set():
                    break
                pcm = await jobs[idx]
                if idx + 1 < len(sentences):
                    jobs.append(asyncio.ensure_future(tts.synthesize(sentences[idx + 1])))
                await self.send_json({"session_id": self.session_id, "type": "tts",
                                      "state": "sentence_start", "text": sentence})
                if self.pcm_mode:
                    step = tts.sample_rate * FRAME_MS // 1000
                    packets = [pcm[i:i + step].tobytes() for i in range(0, len(pcm), step)]
                else:
                    packets = encoder.encode(resample(pcm, tts.sample_rate, DOWN_RATE))
                start = time.monotonic()
                for i, p in enumerate(packets):
                    if self.abort.is_set():
                        break
                    # i primi frame partono subito (buffer), poi ritmo reale
                    delay = start + max(0, i - 5) * FRAME_MS / 1000 - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await self.send_audio(p)
        finally:
            await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
            self.speaking = False
