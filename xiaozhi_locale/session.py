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

from .audio import OpusDecoder, OpusEncoder, make_vad, resample
from .engines import clean_text, split_sentences
from .lessons import MAX_SECONDS as MAX_LESSON_S
from .lessons import is_stop
from .tools import DeviceMCP, ToolGroups, ToolSet

log = logging.getLogger("session")

DOWN_RATE = 24000   # audio verso i dispositivi Opus
FRAME_MS = 60
_END_RE = re.compile(r"[.!?;:…]+[\"')\]]*\s+|\n+")


async def _current_time(name: str, args: dict) -> str:
    now = time.localtime()
    return f"{now.tm_hour}:{now.tm_min:02d}"  # senza parole: così va bene anche per i personaggi in altre lingue


# strumenti interni del server (solo lettura, nessun dato esce dal computer)
LOCAL_TOOLS = ToolSet()
LOCAL_TOOLS.add("ora_attuale", "Restituisce l'ora attuale (ore e minuti). Usalo quando l'utente chiede che ore sono.",
                {"type": "object", "properties": {}}, _current_time)


LANGUAGE_RULES = {
    "en": "IMPORTANT: always answer in English, even when the user writes in Italian.",
    "fr": "IMPORTANT : réponds toujours en français, même si l'utilisateur écrit en italien.",
    "de": "WICHTIG: Antworte immer auf Deutsch, auch wenn der Benutzer auf Italienisch schreibt.",
    "es": "IMPORTANTE: responde siempre en español, aunque el usuario escriba en italiano.",
}


# ricordati anche accanto alla domanda (solo per il modello, poi tolti): con tanti strumenti descritti in italiano
# il modello piccolo tende a rispondere in italiano
LANGUAGE_REMINDERS = {"en": "\n(Answer in English.)", "fr": "\n(Réponds en français.)", "de": "\n(Antworte auf Deutsch.)",
                      "es": "\n(Responde en español.)"}


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
        self.vad = make_vad(self.cfg.get("vad", {}))
        self.history: list[dict] = []
        self.device_mcp = DeviceMCP(self.send_json, self.session_id, app["describe"])
        self.turn_task: asyncio.Task | None = None
        self.timing: dict = {}
        self.mcp_init: asyncio.Task | None = None
        self.opened_groups: set[str] = set()  # gruppi di strumenti aperti dal modello in questa conversazione
        personas = app.get("personas")
        # personaggio: quello assegnato al dispositivo (pagina Admin), altrimenti l'assistente predefinito
        self.persona: dict | None = personas.for_device(self.device_id) if personas is not None else None
        self.lesson: dict | None = None  # modalità lezione: si trascrive senza rispondere
        self.reset_history = False
        self.speaker: str | None = None  # chi parla, riconosciuto dall'impronta vocale (solo dispositivi condivisi)
        self.pending_music = None        # brano da suonare dopo la risposta (strumento musica_riproduci)

    @property
    def tts(self):
        p = self.persona
        if p and "voices" in self.app:
            return self.app["voices"].get(p.get("voice", ""), p.get("speed"))
        return self.app["tts"]

    @property
    def language(self) -> str | None:
        return (self.persona or {}).get("language") or None

    def set_persona(self, persona: dict | None):
        if (persona or {}).get("id") != (self.persona or {}).get("id"):
            self.persona = persona
            # personaggio nuovo: conversazione nuova, dal prossimo turno (non a metà di una risposta in corso)
            self.reset_history = True

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
        if ident is None or ident.get("must_setup_2fa") or ident.get("must_change_password"):
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
            if self.lesson is not None:  # connessione chiusa durante una lezione: si salva quello che c'è
                asyncio.create_task(self.finish_lesson(speak=False))
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
        elif t == "persona":  # client web: scelta del personaggio
            personas = self.app.get("personas")
            p = personas.get(str(msg.get("id") or "")) if personas is not None else None
            self.set_persona(p)
            await self.send_json({"session_id": self.session_id, "type": "persona", "id": (p or {}).get("id", ""),
                                  "name": (p or {}).get("name", self.cfg["assistant"].get("name", "Husky"))})
        elif t == "lesson":  # client web: pulsante della modalità lezione
            if msg.get("state") == "start":
                await self.start_lesson(str(msg.get("title", "")))
            elif msg.get("state") == "stop" and self.lesson is not None:
                asyncio.create_task(self.finish_lesson())

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
            self._signal("vad_end")
            self.finish_utterance()
        elif self.listen_mode != "manual" and self.vad.feed(pcm):
            self.listening = False
            self._signal("vad_end")
            self.finish_utterance()

    def _signal(self, name: str):
        """Avvisa il canale (se lo vuole sapere, es. le schede vocali ESPHome): fine del parlato o niente da capire."""
        cb = getattr(self.ws, "on_session_event", None)
        if cb is not None:
            cb(name)

    def finish_utterance(self):
        if not self.buffer:
            return
        pcm = np.concatenate(self.buffer)
        self.buffer.clear()
        self.buffer_samples = 0
        self.vad.reset()
        if len(pcm) < 16000 * 0.3:
            self.listening = self.listen_mode != "manual"
            self._signal("empty")
            return
        rooms = self.app.get("rooms")
        if rooms is not None and rooms.room_of(self.device_id) is not None:
            # più dispositivi nella stessa aula: risponde solo quello che ha sentito la domanda più forte
            self.launch_turn(pcm, contest=True)
            return
        self.launch_turn(pcm)

    async def yield_room(self):
        """Un altro dispositivo dell'aula ha sentito meglio la domanda: questo chiude l'ascolto in silenzio e torna
        ad aspettare la parola di attivazione."""
        log.info("[%s] Risponde un altro dispositivo della stessa aula", self.device_id)
        self.listening = False
        self.buffer.clear()
        self.buffer_samples = 0
        if getattr(self.ws, "on_session_event", None) is not None:
            self._signal("room_lost")  # schede ESPHome: fine del giro senza risposta
        else:
            await self.ws.close(code=1000, message=b"risponde un altro dispositivo")

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

    def launch_turn(self, item, contest: bool = False):
        if self.turn_task and not self.turn_task.done():
            self.abort.set()
            self.turn_task.cancel()
        self.turn_task = asyncio.create_task(self.turn(item, contest))

    async def _identify(self, pcm) -> str | None:
        """Impronta vocale: chi sta parlando a un dispositivo condiviso (solo persone che hanno dato il consenso)."""
        vp = self.app.get("voiceprints")
        if vp is None or self.role != "device" or not vp.ready():
            return None
        try:
            user, score = await vp.identify(pcm)
        except Exception as e:
            log.warning("Impronta vocale non riuscita: %s", e)
            return None
        if user and user not in self.app["auth"].users.data:
            return None  # account eliminato nel frattempo
        if user:
            log.info("[%s] Voce riconosciuta: %s (%.2f)", self.device_id, user, score)
        else:
            log.info("[%s] Voce non riconosciuta (somiglianza migliore %.2f)", self.device_id, score)
        return user

    @property
    def history_user(self) -> str:
        return self.speaker or self.user

    def _memory_owner(self) -> str | None:
        """Di chi sono i ricordi da usare: della persona riconosciuta dalla voce, altrimenti di chi è collegato."""
        memory = self.app.get("memory")
        if memory is None:
            return None
        if self.speaker:
            role = (self.app["auth"].users.data.get(self.speaker) or {}).get("role", "user")
            return self.speaker if memory.enabled_for(role) else None
        return self.user if memory.enabled_for(self.role) else None

    # ---------------------------------------------------------------- turno di dialogo
    async def turn(self, item, contest: bool = False):
        self.abort.clear()
        if self.reset_history:
            self.reset_history = False
            self.history.clear()
            self.opened_groups.clear()
        cfg = self.cfg
        self.timing = {"start": time.monotonic(), "voice": not isinstance(item, str)}
        self.pending_music = None
        try:
            if not self.refresh_identity():
                await self.revoke("Accesso revocato: apri una nuova sessione")
                return
            if contest:
                loudness = float(np.sqrt(np.mean((item.astype(np.float32) / 32768.0) ** 2)))
                if not await self.app["rooms"].claim(self.device_id, loudness):
                    await self.yield_room()
                    return
            if isinstance(item, str):
                text = item.strip()
            else:
                text, speaker = await asyncio.gather(self.app["asr"].transcribe(item, self.language),
                                                     self._identify(item))
                self.timing["asr"] = time.monotonic()
                if speaker != self.speaker:
                    # persona diversa dalla domanda prima: conversazione nuova (con i suoi ricordi), così nessuno
                    # sente il contesto di qualcun altro
                    self.speaker = speaker
                    self.history.clear()
                    self.opened_groups.clear()
            if not text:
                self._signal("empty")
                await self.resume_listening()
                return
            if self.lesson is not None:  # modalità lezione: si trascrive e basta
                await self.lesson_piece(text)
                return
            if cfg["server"].get("log_transcripts"):
                log.info("[%s] Utente%s: %s", self.device_id, f" ({self.speaker})" if self.speaker else "", text)
            self.app["history"].append(self.history_user, self.device_id, self.session_id, "user", text)
            await self.send_json({"session_id": self.session_id, "type": "stt", "text": text})
            per_min = int((cfg.get("limits") or {}).get("turns_per_minute", 12))
            if not self.app["auth"].limiter.allow(f"turn:{self.user}", per_min, 60):
                log.warning("[%s] troppe domande: limite di %d al minuto", self.user, per_min)
                await self.speak("Stai facendo troppe domande di seguito. Aspetta un momento, per favore.")
                return
            faq = self.app.get("faq")
            hit = faq.match(text) if faq is not None else None
            if hit:  # risposta fissa scritta dall'admin: subito, senza il modello
                answer = hit[0]
                log.info("[%s] Risposta immediata (somiglianza %.2f)", self.device_id, hit[1])
                self.history.append({"role": "user", "content": text})
                self.history.append({"role": "assistant", "content": answer})
                self._trim_history()
                self.app["history"].append(self.history_user, self.device_id, self.session_id, "assistant", answer)
                await self.speak(answer)
                self._record_timing()
                return
            await self.send_json({"session_id": self.session_id, "type": "llm", "emotion": "thinking", "text": "🤔"})

            if self.mcp_init is not None and not self.mcp_init.done():
                # il dispositivo sta ancora mandando l'elenco dei suoi strumenti (volume, schermo...): si aspetta
                # un attimo, altrimenti alla prima domanda il modello non saprebbe di poterli usare
                try:
                    await asyncio.wait_for(asyncio.shield(self.mcp_init), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
            tools = self.build_tools()
            # nel registro eventi finiscono solo gli strumenti che modificano qualcosa (non quelli di sola lettura)
            read_only = set(tools.filtered((cfg.get("tool_permissions") or {}).get("user") or []).names()) \
                | set(LOCAL_TOOLS.names()) | {"promemoria_aggiungi", "promemoria_elenco", "promemoria_rimuovi",
                                           "memoria_salva", "memoria_dimentica", "cerca_documenti", "cambia_assistente",
                                           "musica_elenco", "musica_riproduci"}
            events = self.app.get("events")
            if events is not None:
                tools.on_call = lambda name: None if name in read_only else events.add(
                    "tool", name, self.user, self.req.remote or "")
            camera = any(len(ext.tools) for ext in self.app["external_mcp"]) or any(
                "husky" in name and len(ts) for name, ts in (self.app["mcp_hub"].toolsets_for(self.role)
                                                             if "mcp_hub" in self.app else []))
            # il prompt di sistema dice al modello se la telecamera è collegata in questo momento
            base = (self.persona or {}).get("prompt") or cfg["assistant"]["system_prompt"]
            system = base.strip() + "\n" + (
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
            if self.speaker:
                system += f"\nStai parlando con {self.speaker} (riconosciuto dalla voce)."
            owner = self._memory_owner()
            if owner:
                remembered = self.app["memory"].prompt(owner)
                if remembered:
                    system += "\n" + remembered
            rule = LANGUAGE_RULES.get((self.persona or {}).get("language", ""))
            if rule:  # personaggio in un'altra lingua: provato col modello piccolo, la regola va in fondo
                system += "\n" + rule
            if not self.history:
                self.history.append({"role": "system", "content": system})
            else:
                self.history[0] = {"role": "system", "content": system}
            # documenti del prof: se la ricerca trova un pezzo molto pertinente lo si mette accanto alla domanda (il
            # modello piccolo da solo non cerca quasi mai); il modello lo usa solo se serve. Poi si toglie dalla memoria.
            reminder = LANGUAGE_REMINDERS.get((self.persona or {}).get("language", ""), "")
            user_msg = {"role": "user", "content": text + self._document_context(text) + reminder}
            self.history.append(user_msg)
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
                user_msg["content"] = text
                self._trim_history()
                if cfg["server"].get("log_transcripts"):
                    log.info("[%s] Assistente: %s", self.device_id, answer)
                if answer:
                    self.app["history"].append(self.history_user, self.device_id, self.session_id, "assistant", answer)
                self._record_timing()
                owner = self._memory_owner()
                if owner:
                    # anche se il modello ha salvato qualcosa (spesso incompleto): se la frase sembra personale, si
                    # estrae in sottofondo; i doppioni vengono scartati
                    asyncio.create_task(self.app["memory"].extract(self.app["llm"], owner, text))
            if self.pending_music is not None and not self.abort.is_set():
                path, self.pending_music = self.pending_music, None
                await self.play_music(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Errore nel turno")
        self.pending_music = None
        if self.speaking:  # per sicurezza: il dispositivo non deve restare bloccato in "parla"
            await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
            self.speaking = False
        await self.resume_listening()

    def _document_context(self, text: str) -> str:
        knowledge = self.app.get("knowledge")
        groups = (self.persona or {}).get("groups")
        if knowledge is None or (groups and "documenti" not in groups):
            return ""
        return knowledge.context_for(text, self.role)

    def build_tools(self) -> ToolSet:
        """Gli strumenti che il modello può usare in questo turno, divisi per gruppo.

        Permessi: ogni ruolo vede solo gli strumenti che può usare (tool_permissions in config; gli strumenti
        personalizzati hanno i loro ruoli). Se sono più di assistant.tool_groups_threshold, il modello vede
        l'elenco dei gruppi e apre solo quelli che gli servono.
        """
        cfg = self.cfg
        perms = (cfg.get("tool_permissions") or {}).get(self.role)

        def allowed(ts: ToolSet) -> ToolSet:
            return ts.filtered(perms) if perms is not None else ts

        groups: dict[str, tuple[str, ToolSet]] = {}

        def describe(label: str, ts: ToolSet) -> str:
            names = ts.names()
            return f"{label} ({', '.join(names[:6])}{'…' if len(names) > 6 else ''})"

        dev = allowed(self.device_mcp.tools)
        groups["dispositivo"] = (describe("controlli di questo dispositivo, es. volume e schermo", dev), dev)
        for ext in self.app["external_mcp"]:
            ts = allowed(ext.tools)
            label = "telecamera HuskyLens: cosa vede, riconoscimento, modelli" if "husky" in ext.name.lower() \
                else f"strumenti di {ext.name}"
            groups[ext.name] = (describe(label, ts), ts)
        mcp_hub = self.app.get("mcp_hub")
        if mcp_hub is not None:  # server MCP e punti di accesso aggiunti dalla pagina Admin (con i loro ruoli)
            for name, ts in mcp_hub.toolsets_for(self.role):
                groups[name] = (describe(f"strumenti di {name}", ts), ts)
        esp = ToolSet()
        hub = self.app.get("mqtt")
        if hub is not None:  # dispositivi ESPHome riconosciuti in automatico via MQTT
            esp.merge(hub.toolset_for(self.role))
        native = self.app.get("esphome")
        if native is not None:  # dispositivi ESPHome collegati con l'API nativa
            esp.merge(native.toolset_for(self.role))
        groups["esphome"] = (describe("dispositivi ESPHome del laboratorio: sensori e comandi", esp), esp)
        notes = self.app.get("notes")
        if notes is not None:
            ts = notes.toolset(self.user)
            groups["promemoria"] = (describe("promemoria e note dell'utente", ts), ts)
        knowledge = self.app.get("knowledge")
        if knowledge is not None:  # documenti caricati dal prof
            ts = knowledge.toolset(self.role)
            groups["documenti"] = (describe("documenti del prof: appunti, regolamenti, materiali", ts), ts)
        music = self.app.get("music")
        if music is not None and music.allowed(self.role):  # canzoni e audio della cartella del server
            ts = music.toolset(self)
            groups["musica"] = (describe("musica e audio del server", ts), ts)
        custom = self.app.get("custom_tools")
        if custom is not None:  # strumenti creati dall'admin (comandi, web, MQTT), se attivati per questo ruolo
            for kind, (name, label) in {"command": ("comandi", "comandi sul server"),
                                        "web": ("web", "informazioni da siti web"),
                                        "mqtt": ("sensori", "sensori e dispositivi della scuola")}.items():
                ts = custom.toolset_for(self.role, kind)
                groups[name] = (describe(label, ts), ts)
        allowed_groups = (self.persona or {}).get("groups")
        if allowed_groups:  # il personaggio usa solo i suoi gruppi di strumenti
            groups = {k: v for k, v in groups.items() if k in allowed_groups}
        always = ToolSet()
        always.merge(LOCAL_TOOLS)  # strumenti interni del server, di sola lettura: per tutti
        always.merge(self._session_tools())
        total = sum(len(ts) for _, ts in groups.values())
        threshold = int(cfg["assistant"].get("tool_groups_threshold", 12))
        if threshold and total > threshold:
            return ToolGroups(groups, always, self.opened_groups)
        tools = ToolSet()
        for _, ts in groups.values():
            tools.merge(ts)
        tools.merge(always)
        return tools

    def _session_tools(self) -> ToolSet:
        """Strumenti legati a questa conversazione: memoria, cambio di personaggio, modalità lezione."""
        ts = ToolSet()
        owner = self._memory_owner()
        if owner:
            ts.merge(self.app["memory"].toolset(owner))
        personas = self.app.get("personas")
        if personas is not None and personas.items():
            names = [p["name"] for p in personas.items()]
            default = self.cfg["assistant"].get("name", "Husky")

            async def switch(_n, a):
                wanted = str(a.get("nome", ""))
                if wanted.strip().lower() in (default.lower(), "predefinito", "normale"):
                    self.set_persona(None)
                    return f"Fatto: dalla prossima domanda risponde {default}."
                p = personas.find(wanted)
                if p is None:
                    return f"Non esiste. Assistenti disponibili: {default}, {', '.join(names)}."
                self.set_persona(p)
                return f"Fatto: dalla prossima domanda risponde {p['name']}. Salutalo in breve come {p['name']}."
            ts.add("cambia_assistente", f"Passa a un altro assistente quando l'utente lo chiede. Assistenti: {default}, "
                   f"{', '.join(names)}.", {"type": "object", "properties": {"nome": {"type": "string", "maxLength": 60}},
                                            "required": ["nome"]}, switch)
        lessons = self.app.get("lessons")
        if lessons is not None and lessons.allowed(self.role):
            async def lesson(_n, a):
                if self.lesson is not None:
                    return "La modalità lezione è già attiva."
                await self.start_lesson(str(a.get("titolo", "")), announce=False)
                return ("Modalità lezione attiva: da ora trascrivo la lezione senza rispondere. Per finire basta dire "
                        "'fine della lezione'. Dillo all'utente in una frase.")
            ts.add("lezione_inizia", "Avvia la modalità lezione: Husky trascrive la lezione senza rispondere e alla fine "
                   "prepara il riassunto. Usalo quando il prof chiede di iniziare la lezione o la modalità lezione.",
                   {"type": "object", "properties": {"titolo": {"type": "string", "maxLength": 100,
                                                                "description": "argomento, se detto"}}}, lesson)
        return ts

    # ---------------------------------------------------------------- modalità lezione
    async def start_lesson(self, title: str = "", announce: bool = True):
        lessons = self.app.get("lessons")
        if lessons is None or not lessons.allowed(self.role):
            await self.send_json({"session_id": self.session_id, "type": "lesson", "state": "denied"})
            return
        if self.lesson is None:
            self.lesson = lessons.new(self.user, self.device_id, title)
            log.info("[%s] Modalità lezione avviata da %s", self.device_id, self.user)
            self.app["events"].add("lesson", f"avviata: {self.lesson['title']}", self.user, self.req.remote or "")
        await self.send_json({"session_id": self.session_id, "type": "lesson", "state": "started",
                              "id": self.lesson["id"], "title": self.lesson["title"]})
        if announce:
            await self.resume_listening()

    async def lesson_piece(self, text: str):
        lesson = self.lesson
        if is_stop(text):
            await self.finish_lesson()
            return
        lesson["parts"].append({"t": round(time.time() - lesson["start"]), "text": text})
        await self.send_json({"session_id": self.session_id, "type": "lesson", "state": "text", "text": text})
        if time.time() - lesson["start"] > MAX_LESSON_S:
            await self.finish_lesson()
            return
        await self.resume_listening()

    async def finish_lesson(self, speak: bool = True):
        lesson, self.lesson = self.lesson, None
        if lesson is None:
            return
        await self.send_json({"session_id": self.session_id, "type": "lesson", "state": "summarizing"})
        transcript = "\n".join(p["text"] for p in lesson["parts"])
        try:
            summary = await self.app["lessons"].summarize(self.app["llm"], transcript)
        except Exception as e:
            log.warning("Riassunto della lezione non riuscito: %s", e)
            summary = "Riassunto non disponibile: il modello linguistico non ha risposto. La trascrizione è salvata."
        item = self.app["lessons"].save(lesson, summary)
        self.app["events"].add("lesson", f"salvata: {item['title']} ({item['words']} parole)", self.user,
                               self.req.remote or "")
        await self.send_json({"session_id": self.session_id, "type": "lesson", "state": "done", "id": item["id"],
                              "title": item["title"], "summary": summary})
        if speak and not self.ws.closed:
            await self.speak("Lezione finita. Ho salvato la trascrizione e il riassunto nella pagina Lezioni.")
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
        rec = {
            "time": time.time(), "voice": t["voice"],
            "asr": round(t["asr"] - t["start"], 2) if "asr" in t else None,
            "llm_first": round(t["llm_first"] - t.get("asr", t["start"]), 2) if "llm_first" in t else None,
            "first_audio": round(t["first_audio"] - t["start"], 2),
        }
        self.app["stats"].append(rec)
        if "metrics" in self.app:
            self.app["metrics"].record_turn(rec)

    async def play_music(self, path):
        """Suona un file audio del server sul dispositivo (o nella pagina web), al ritmo reale, a pezzi da un secondo.
        Si ferma con la parola di attivazione, il pulsante, oppure dopo music.max_minutes."""
        from .music import decode_chunks
        import threading
        mcfg = self.cfg.get("music") or {}
        rate = self.tts.sample_rate if self.pcm_mode else DOWN_RATE
        frame = rate * FRAME_MS // 1000
        stop = threading.Event()
        gen = decode_chunks(path, rate, stop, float(mcfg.get("max_minutes", 15)) * 60)
        loop = asyncio.get_running_loop()
        encoder = None if self.pcm_mode else OpusEncoder(DOWN_RATE, FRAME_MS)
        title = path.stem.replace("_", " ")
        log.info("[%s] Musica: %s", self.device_id, path.name)
        events = self.app.get("events")
        if events is not None:
            events.add("music", title[:80], self.history_user, self.req.remote or "")
        self.speaking = True
        await self.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
        await self.send_json({"session_id": self.session_id, "type": "tts", "state": "sentence_start",
                              "text": f"♪ {title}"})
        carry = np.zeros(0, dtype=np.int16)
        start, sent = time.monotonic(), 0
        try:
            while not self.abort.is_set():
                try:
                    pcm = await loop.run_in_executor(None, next, gen, None)
                except Exception as e:
                    log.warning("Musica: file non leggibile (%s): %s", path.name, e)
                    break
                if pcm is None:
                    break
                carry = np.concatenate([carry, pcm])
                n = len(carry) // frame * frame
                piece, carry = carry[:n], carry[n:]
                packets = ([piece[i:i + frame].tobytes() for i in range(0, n, frame)] if self.pcm_mode
                           else encoder.encode(piece))
                for p in packets:
                    if self.abort.is_set():
                        break
                    delay = start + max(0, sent - 5) * FRAME_MS / 1000 - time.monotonic()
                    if delay > 0 and getattr(self.ws, "realtime", True):
                        await asyncio.sleep(delay)
                    await self.send_audio(p)
                    sent += 1
        finally:
            stop.set()
            try:
                gen.close()
            except ValueError:  # il pezzo successivo si sta ancora decodificando: si chiude da solo con stop
                pass
            await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
            self.speaking = False

    async def speak(self, text: str):
        async def one_by_one():
            for s in split_sentences(text) or ["..."]:
                yield s
        await self.speak_stream(one_by_one())

    async def speak_stream(self, sentences, first_emotion: bool = False):
        """Legge le frasi man mano che arrivano: mentre una viene inviata, si prepara la successiva
        (il modello continua a scrivere e la sintesi vocale lavora in parallelo)."""
        tts = self.tts
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
                    if delay > 0 and getattr(self.ws, "realtime", True):  # un file da scaricare non aspetta
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
            if self.pending_music is None or self.abort.is_set():
                await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
                self.speaking = False
            # altrimenti subito dopo parte la musica: il dispositivo resta in "parla" senza riaprire il microfono
