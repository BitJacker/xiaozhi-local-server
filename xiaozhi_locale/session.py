"""Gestione di una connessione WebSocket secondo il protocollo xiaozhi.

Flusso: hello -> listen(start) -> audio Opus -> [fine frase] -> stt -> llm -> tts(start,
sentence_start, audio..., stop). Vedi docs/websocket.md del progetto xiaozhi-esp32.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import time
import uuid
from collections.abc import Callable

import numpy as np
from aiohttp import WSMsgType, web

from .audio import EnergyVAD, OpusDecoder, OpusEncoder, resample
from .engines import clean_text, split_sentences
from .tools import DeviceMCP, ToolSet

log = logging.getLogger("session")

DOWN_RATE = 24000   # audio verso i dispositivi Opus
FRAME_MS = 60
_END_RE = re.compile(r"[.!?;:…]+[\"')\]]*\s+|\n+")


async def _current_time(name: str, args: dict) -> str:
    now = time.localtime()
    return f"Sono le {now.tm_hour}:{now.tm_min:02d}."


# strumenti interni del server (solo lettura, nessun dato esce dal computer)
LOCAL_TOOLS = ToolSet()
LOCAL_TOOLS.add("ora_attuale", "Restituisce l'ora attuale (ore e minuti). Usalo quando l'utente chiede che ore sono.",
                {"type": "object", "properties": {}}, _current_time)


def take_sentences(buf: str, min_len: int = 12, comma_after: int = 90) -> tuple[list[str], str]:
    """Dal testo che il modello sta scrivendo estrae le frasi già complete; ritorna (frasi, resto).

    Una frase è pronta quando finisce con . ! ? ; : … seguito da spazio o a capo (e non è troppo corta).
    Se una frase è molto lunga si spezza a una virgola, così la voce parte prima.
    """
    out = []
    while True:
        cut = None
        for m in _END_RE.finditer(buf):
            if len(buf[:m.end()].strip()) >= min_len:
                cut = m.end()
                break
        if cut is None and len(buf) > comma_after:
            comma = buf.rfind(", ", min_len, len(buf))
            if comma > 0:
                cut = comma + 2
        if cut is None:
            return out, buf
        # una frase corta resta unita alla successiva ("Ciao! Oggi è..."): un solo pezzo di voce, più naturale
        piece = clean_text(buf[:cut].replace("\n", " "))
        if re.search(r"\w", piece):
            out.append(piece)
        buf = buf[cut:]


class Session:
    def __init__(self, app: web.Application, ws: web.WebSocketResponse, request: web.Request, user: str = "locale",
                 role: str = "admin", device_token: str = "",
                 identity_check: Callable[[], dict | None] | None = None):
        self.app = app
        self.cfg = app["cfg"]
        self.ws = ws
        self.req = request
        self.session_id = str(uuid.uuid4())
        self.user = user
        self.role = role
        self.device_token = device_token
        # L'identità viene verificata di nuovo prima di ogni turno: una revoca o un cambio di ruolo
        # deve valere anche per un WebSocket già aperto.
        self.identity_check = identity_check
        self.device_id = request.headers.get("Device-Id") or request.query.get("device-id") or "web"
        self.protocol_version = int(request.headers.get("Protocol-Version", "1") or 1)

        self.pcm_mode = False          # True per il client web (PCM grezzo, niente Opus)
        self.decoder: OpusDecoder | None = None
        self.listen_mode = "auto"
        self.listening = False
        self.speaking = False
        self.abort = asyncio.Event()
        self.buffer: list[np.ndarray] = []
        self.buffer_samples = 0
        vcfg = self.cfg.get("vad", {})
        self.vad = EnergyVAD(vcfg.get("silence_ms", 800), vcfg.get("min_speech_ms", 300),
                             vcfg.get("energy_threshold", 0.012), vcfg.get("max_utterance_s", 20))
        self.history: list[dict] = []
        self.device_mcp = DeviceMCP(self.send_json, self.session_id, app["describe"])
        self.turn_task: asyncio.Task | None = None
        self.timing: dict = {}
        self.mcp_init: asyncio.Task | None = None

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

    def refresh_identity(self) -> bool:
        """Aggiorna l'identità dal token originale; False se è stata revocata."""
        if self.identity_check is None:
            return True
        ident = self.identity_check()
        if ident is None or ident.get("must_setup_2fa"):
            return False
        self.user = ident["username"]
        self.role = ident.get("role", "user")
        self.device_token = ident.get("token", self.device_token)
        return True

    async def revoke(self, reason: str = "Accesso revocato"):
        """Ferma subito un socket che non è più autorizzato."""
        self.abort.set()
        task = self.turn_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if not self.ws.closed:
            await self.ws.close(code=1008, message=reason.encode("utf-8"))

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
            self.history.clear()  # memoria di lavoro del modello azzerata (la cronologia su disco resta)
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
            self.mcp_init = asyncio.create_task(self._init_device_mcp())

    async def _init_device_mcp(self):
        try:
            # il dispositivo usa il SUO token anche per caricare le foto da analizzare (/xiaozhi/vision/explain)
            token = self.device_token or self.app["token_for_devices"]
            await self.device_mcp.initialize(self.app["vision_url"], token)
        except Exception as e:
            log.warning("MCP dispositivo non disponibile: %r", e)

    # ---------------------------------------------------------------- ascolto
    def start_listening(self):
        self.listening = True
        self.buffer.clear()
        self.buffer_samples = 0
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
        self.buffer_samples += len(pcm)
        # Anche il push-to-talk ha un tetto: senza questo controllo un client può inviare audio
        # all'infinito senza mai mandare "listen stop".
        if self.buffer_samples >= self.vad.max_samples:
            self.listening = False
            self.finish_utterance()
        elif self.listen_mode != "manual" and self.vad.feed(pcm):
            self.listening = False
            self.finish_utterance()

    def finish_utterance(self):
        if not self.buffer:
            return
        pcm = np.concatenate(self.buffer)
        self.buffer.clear()
        self.buffer_samples = 0
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
        self.timing = {"start": time.monotonic(), "voice": not isinstance(item, str)}
        try:
            if not self.refresh_identity():
                await self.revoke("Accesso revocato: apri una nuova sessione")
                return
            if isinstance(item, str):
                text = item.strip()
            else:
                text = await self.app["asr"].transcribe(item)
                self.timing["asr"] = time.monotonic()
            if not text:
                await self.resume_listening()
                return
            if cfg["server"].get("log_transcripts"):
                log.info("[%s] Utente: %s", self.device_id, text)
            self.app["history"].append(self.user, self.device_id, self.session_id, "user", text)
            await self.send_json({"session_id": self.session_id, "type": "stt", "text": text})
            per_min = int((cfg.get("limits") or {}).get("turns_per_minute", 12))
            if not self.app["auth"].limiter.allow(f"turn:{self.user}", per_min, 60):
                log.warning("[%s] troppe domande: limite di %d al minuto", self.user, per_min)
                await self.speak("Stai facendo troppe domande di seguito. Aspetta un momento, per favore.")
                return
            await self.send_json({"session_id": self.session_id, "type": "llm", "emotion": "thinking", "text": "🤔"})

            if self.mcp_init is not None and not self.mcp_init.done():
                # il dispositivo sta ancora mandando l'elenco dei suoi strumenti (volume, schermo...): si aspetta
                # un attimo, altrimenti alla prima domanda il modello non saprebbe di poterli usare
                try:
                    await asyncio.wait_for(asyncio.shield(self.mcp_init), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
            tools = ToolSet()
            tools.merge(self.device_mcp.tools)
            for ext in self.app["external_mcp"]:
                tools.merge(ext.tools)
            # permessi: ogni ruolo vede solo gli strumenti che può usare (tool_permissions in config)
            perms = (cfg.get("tool_permissions") or {}).get(self.role)
            if perms is not None:
                tools = tools.filtered(perms)
            tools.merge(LOCAL_TOOLS)  # strumenti interni del server, di sola lettura: per tutti
            # nel registro eventi finiscono solo gli strumenti che modificano qualcosa (non quelli di sola lettura)
            read_only = set(tools.filtered((cfg.get("tool_permissions") or {}).get("user") or []).names()) \
                | set(LOCAL_TOOLS.names())
            events = self.app.get("events")
            if events is not None:
                tools.on_call = lambda name: None if name in read_only else events.add(
                    "tool", name, self.user, self.req.remote or "")
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
            # solo la data: l'ora cambia ogni minuto e renderebbe diverso l'inizio della conversazione a ogni turno
            # (Ollama non potrebbe riusare i calcoli già fatti). L'ora si chiede con lo strumento ora_attuale.
            system += f"\nOggi è {giorni[now.tm_wday]} {now.tm_mday:02d}/{now.tm_mon:02d}/{now.tm_year}."
            if not self.history:
                self.history.append({"role": "system", "content": system})
            else:
                self.history[0] = {"role": "system", "content": system}
            self.history.append({"role": "user", "content": text})
            spoken: list[str] = []
            produced: list[str] = []
            timing = self.timing

            async def sentences():
                """Frasi pronte da leggere, mentre il modello continua a scrivere."""
                buf = ""
                try:
                    async for piece in self.app["llm"].chat_stream(self.history, tools):
                        if "llm_first" not in timing:
                            timing["llm_first"] = time.monotonic()
                        produced.append(piece)
                        buf += piece
                        ready, buf = take_sentences(buf)
                        for s in ready:
                            spoken.append(s)
                            yield s
                except Exception as e:
                    log.error("LLM non raggiungibile: %s", e)
                    buf = "Il modello linguistico locale non risponde. Controlla che il server LLM sia avviato."
                for s in split_sentences(buf):
                    spoken.append(s)
                    yield s

            try:
                await self.speak_stream(sentences(), first_emotion=True)
            finally:
                # in cronologia va tutto quello che il modello ha scritto (anche se interrotto prima di leggerlo)
                full = "".join(produced).strip()
                answer = clean_text(full) if full else clean_text(" ".join(spoken))
                if self.abort.is_set():
                    answer = f"{answer} (interrotta)" if answer else "(risposta interrotta)"
                self._trim_history()
                if cfg["server"].get("log_transcripts"):
                    log.info("[%s] Assistente: %s", self.device_id, answer)
                if answer:
                    self.app["history"].append(self.user, self.device_id, self.session_id, "assistant", answer)
                self._record_timing()
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

    def _record_timing(self):
        """Tempi del turno per la pagina Admin (solo in memoria, nessun contenuto della conversazione)."""
        t = self.timing
        if "first_audio" not in t:
            return
        self.app["stats"].append({
            "time": time.time(), "voice": t["voice"],
            "asr": round(t["asr"] - t["start"], 2) if "asr" in t else None,
            "llm_first": round(t["llm_first"] - t.get("asr", t["start"]), 2) if "llm_first" in t else None,
            "first_audio": round(t["first_audio"] - t["start"], 2),
        })

    async def speak(self, text: str):
        async def one_by_one():
            for s in split_sentences(text) or ["..."]:
                yield s
        await self.speak_stream(one_by_one())

    async def speak_stream(self, sentences, first_emotion: bool = False):
        """Legge le frasi man mano che arrivano: mentre una viene inviata, si prepara la successiva
        (il modello continua a scrivere e la sintesi vocale lavora in parallelo)."""
        tts = self.app["tts"]
        self.speaking = True
        await self.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
        it = sentences.__aiter__()
        pending: list[asyncio.Future] = []

        async def next_job():
            try:
                s = await it.__anext__()
            except StopAsyncIteration:
                return None
            return s, asyncio.ensure_future(tts.synthesize(s))

        try:
            encoder = None if self.pcm_mode else OpusEncoder(DOWN_RATE, FRAME_MS)
            nxt = await next_job()
            while nxt is not None and not self.abort.is_set():
                sentence, job = nxt
                pending.append(job)
                pcm = await job
                ahead = asyncio.ensure_future(next_job())  # frase successiva in preparazione
                pending.append(ahead)
                if first_emotion:
                    first_emotion = False
                    await self.send_json({"session_id": self.session_id, "type": "llm", "emotion": "happy", "text": "🙂"})
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
                    if i == 0 and hasattr(self, "timing"):
                        self.timing.setdefault("first_audio", time.monotonic())
                nxt = await ahead
        finally:
            for f in pending:
                if not f.done():
                    f.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            aclose = getattr(it, "aclose", None)
            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass
            await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
            self.speaking = False
