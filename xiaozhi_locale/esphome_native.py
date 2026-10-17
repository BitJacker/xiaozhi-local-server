"""Dispositivi ESPHome collegati con l'API nativa (la stessa di Home Assistant), senza MQTT.

* **Scoperta automatica** in rete locale (mDNS, _esphomelib._tcp): si accende dalla pagina Admin, che mostra i
  dispositivi trovati. Nessun dispositivo viene usato finché l'admin non lo approva.
* Collegamento **cifrato** (Noise, chiave `api: encryption: key:` del file YAML), ricollegamento automatico.
* Sensori, interruttori, luci, pulsanti, numeri e selettori diventano strumenti di Husky (leggere: tutti; comandare:
  i ruoli in esphome.command_roles).
* **Schede vocali** (voice_assistant: ESP32-S3-BOX, Atom Echo, Voice PE...): Husky fa da assistente al posto di
  Home Assistant. La scheda sente la parola di attivazione e manda la voce; Husky trascrive, risponde e restituisce
  la voce (in streaming sull'altoparlante, oppure come file WAV per i media player). Stessa conversazione, strumenti
  e permessi di un dispositivo xiaozhi (ruolo "device").
Impostazioni in data/esphome.json (chiavi e password dei dispositivi cifrate).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import secrets
import time
import wave
from pathlib import Path

import numpy as np
from aiohttp import WSMsgType

from .audio import resample
from .auth import JsonStore
from .tools import ToolSet

log = logging.getLogger("esphome")

SERVICE = "_esphomelib._tcp.local."
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,253}$")
AUDIO_TTL = 600
COMMAND_ROLES = ["admin", "device"]


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (s or "").lower()).strip("_")[:30] or "x"


def make_wav(pcm: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


class _Msg:
    def __init__(self, type_, data):
        self.type, self.data = type_, data


class _Req:
    """Quello che la Session legge dalla richiesta WebSocket."""

    def __init__(self, node: str, ip: str):
        self.headers = {"Device-Id": f"esphome:{node}", "Client-Id": node, "Protocol-Version": "1"}
        self.query = {}
        self.remote = ip


class EspChannel:
    """Si comporta come il WebSocket di aiohttp per la Session e traduce verso gli eventi voice_assistant."""

    def __init__(self, dev: "EspDevice"):
        self.dev = dev
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.realtime = True  # in streaming la voce va al ritmo reale; per un file WAV si prepara tutta subito

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def send_str(self, text: str):
        try:
            obj = json.loads(text)
        except ValueError:
            return
        run = self.dev.run
        if run is not None:
            await self.dev.on_session_message(obj)

    async def send_bytes(self, pcm: bytes):
        if self.dev.run is not None:
            self.dev.on_session_audio(pcm)

    async def close(self, code: int = 1000, message: bytes = b""):
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def on_session_event(self, name: str):
        """Dalla Session: fine del parlato (vad_end) oppure niente di comprensibile (empty)."""
        if self.dev.run is not None:
            self.dev.on_vad(name)

    def feed_text(self, obj: dict):
        self.queue.put_nowait(_Msg(WSMsgType.TEXT, json.dumps(obj)))

    def feed_audio(self, pcm: bytes):
        self.queue.put_nowait(_Msg(WSMsgType.BINARY, pcm))


class EspDevice:
    def __init__(self, mgr: "EsphomeManager", cfg: dict):
        self.mgr = mgr
        self.cfg = cfg
        self.client = None
        self.logic = None
        self.info = None
        self.entities: dict[int, object] = {}
        self.states: dict[int, tuple[object, float]] = {}
        self.connected = False
        self.last_error = ""
        self.session = None
        self.channel: EspChannel | None = None
        self.run: dict | None = None
        self._unsub_voice = None

    @property
    def node(self) -> str:
        return self.cfg["name"]

    # ------------------------------------------------------------------ collegamento
    async def start(self):
        from aioesphomeapi import APIClient, ReconnectLogic
        box = self.mgr.box
        dec = (lambda v: box.decrypt(v) if (box and v) else v)
        self.client = APIClient(self.cfg["host"], int(self.cfg.get("port") or 6053), dec(self.cfg.get("password")) or None,
                                client_info="Husky locale", noise_psk=dec(self.cfg.get("key")) or None,
                                zeroconf_instance=self.mgr.zeroconf())
        self.logic = ReconnectLogic(client=self.client, on_connect=self._on_connect, on_disconnect=self._on_disconnect,
                                    zeroconf_instance=self.mgr.zeroconf(), name=self.cfg.get("node") or None,
                                    on_connect_error=self._on_error)
        await self.logic.start()

    async def stop(self):
        await self._close_session()
        if self.logic:
            await self.logic.stop()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.connected = False

    async def _on_error(self, err: Exception):
        from aioesphomeapi import InvalidAuthAPIError, InvalidEncryptionKeyAPIError, RequiresEncryptionAPIError
        if isinstance(err, RequiresEncryptionAPIError):
            self.last_error = "il dispositivo usa la cifratura: scrivi la chiave (api: encryption: key:)"
        elif isinstance(err, InvalidEncryptionKeyAPIError):
            self.last_error = "chiave di cifratura sbagliata"
        elif isinstance(err, InvalidAuthAPIError):
            self.last_error = "password sbagliata"
        else:
            self.last_error = str(err) or type(err).__name__
        log.warning("ESPHome '%s' (%s): %s", self.node, self.cfg["host"], self.last_error)

    async def _on_connect(self):
        from aioesphomeapi import VoiceAssistantFeature
        info, ents, _ = await self.client.device_info_and_list_entities()
        self.info = info
        self.entities = {e.key: e for e in ents}
        self.states.clear()
        self.client.subscribe_states(self._on_state)
        self.connected, self.last_error = True, ""
        flags = info.voice_assistant_feature_flags or 0
        if self.cfg.get("voice") and flags & VoiceAssistantFeature.VOICE_ASSISTANT:
            self._unsub_voice = self.client.subscribe_voice_assistant(
                handle_start=self._voice_start, handle_stop=self._voice_stop, handle_audio=self._voice_audio)
        log.info("ESPHome '%s' collegato (%s, ESPHome %s): %d entità%s", self.node, info.model, info.esphome_version,
                 len(self.entities), ", assistente vocale" if self._unsub_voice else "")

    async def _on_disconnect(self, expected: bool):
        self.connected = False
        self._unsub_voice = None
        self.run = None
        await self._close_session()

    def _on_state(self, st):
        self.states[st.key] = (st, time.time())

    # ------------------------------------------------------------------ strumenti
    def _kind(self, e) -> str:
        return type(e).__name__.removesuffix("Info").lower()

    def _value(self, e, st) -> str:
        kind = self._kind(e)
        if kind in ("binarysensor", "switch", "light", "fan"):
            on = bool(getattr(st, "state", False))
            txt = "acceso / sì" if on else "spento / no"
            if kind == "light" and on and getattr(st, "brightness", None) is not None:
                txt += f", luminosità {round(st.brightness * 100)}%"
            return txt
        if getattr(st, "missing_state", False):
            return "nessun valore"
        v = getattr(st, "state", "")
        if isinstance(v, float):
            v = round(v, max(0, int(getattr(e, "accuracy_decimals", 1) or 0)))
        unit = getattr(e, "unit_of_measurement", "") or ""
        return f"{v}{(' ' + unit) if unit else ''}"

    def read_all(self) -> str:
        if not self.connected:
            return f"Errore: il dispositivo {self.cfg.get('label') or self.node} non è collegato"
        lines = []
        for key, e in self.entities.items():
            if self._kind(e) not in ("sensor", "binarysensor", "textsensor", "switch", "light", "fan", "number",
                                     "select"):
                continue
            st = self.states.get(key)
            if st is None:
                lines.append(f"{e.name}: nessun valore ancora")
            else:
                lines.append(f"{e.name}: {self._value(e, st[0])} (di {int(time.time() - st[1])} s fa)")
        return "\n".join(lines) or "Nessun dato da questo dispositivo."

    async def command(self, key: int, args: dict) -> str:
        if not self.connected:
            return "Errore: dispositivo non collegato"
        e = self.entities.get(key)
        kind = self._kind(e) if e else ""
        c = self.client
        if kind == "button":
            c.button_command(key)
        elif kind == "number":
            c.number_command(key, float(args["valore"]))
        elif kind == "select":
            c.select_command(key, str(args["valore"]))
        elif kind in ("switch", "light", "fan"):
            on = str(args.get("stato", "")).lower() in ("on", "acceso", "accendi", "true", "1")
            if kind == "switch":
                c.switch_command(key, on)
            elif kind == "fan":
                c.fan_command(key, state=on)
            else:
                b = args.get("luminosita")
                c.light_command(key, state=on, brightness=(int(b) / 100) if (on and b is not None) else None)
        else:
            return "Errore: comando non disponibile"
        return "Comando inviato."

    def toolset(self, role: str, command_roles: list[str]) -> ToolSet:
        ts = ToolSet()
        if not self.connected:
            return ts
        label = self.cfg.get("label") or (self.info.friendly_name if self.info else "") or self.node
        readable = [e.name for e in self.entities.values()
                    if self._kind(e) in ("sensor", "binarysensor", "textsensor", "switch", "light", "fan")]
        if readable:
            async def read(_n, _a):
                return self.read_all()
            ts.add(f"esphome_{self.node}", f"Legge i sensori e lo stato del dispositivo {label}: "
                   f"{', '.join(readable)[:300]}", {"type": "object", "properties": {}}, read)
        if role not in command_roles:
            return ts
        for key, e in self.entities.items():
            kind = self._kind(e)
            props: dict = {}
            if kind in ("switch", "light", "fan"):
                props["stato"] = {"type": "string", "enum": ["on", "off"], "description": "on per accendere, off per spegnere"}
                if kind == "light":
                    props["luminosita"] = {"type": "integer", "minimum": 1, "maximum": 100, "description": "percentuale (facoltativa)"}
            elif kind == "number":
                props["valore"] = {"type": "number", "minimum": e.min_value, "maximum": e.max_value,
                                   "description": e.unit_of_measurement or ""}
            elif kind == "select":
                props["valore"] = {"type": "string", "enum": list(e.options)[:50]}
            elif kind != "button":
                continue
            verb = {"button": "Preme il pulsante", "number": "Imposta", "select": "Sceglie"}.get(kind, "Accende o spegne")

            async def cmd(_n, a, _key=key):
                return await self.command(_key, a)
            ts.add(f"{self.node}_{_safe(e.object_id or e.name)}", f"{verb} '{e.name}' del dispositivo {label}",
                   {"type": "object", "properties": props, "required": [k for k in props if k != "luminosita"]}, cmd)
        return ts

    # ------------------------------------------------------------------ assistente vocale
    def _features(self) -> int:
        return (self.info.voice_assistant_feature_flags or 0) if self.info else 0

    async def _ensure_session(self):
        if self.session is not None and self.channel is not None and not self.channel.closed:
            return
        from .session import Session
        app = self.mgr.app
        self.channel = ch = EspChannel(self)
        name = self.cfg.get("label") or self.node

        def identity():
            cur = self.mgr.device_cfg(self.cfg["id"])
            ok = cur is not None and cur.get("voice") and self.connected
            return {"username": cur.get("label") or cur["name"], "role": "device", "token": ""} if ok else None

        self.session = s = Session(app, ch, _Req(self.node, self.cfg["host"]), user=name, role="device",
                                   identity_check=identity)
        app["active_sessions"].add(s)
        ch.feed_text({"type": "hello", "version": 1, "audio_params": {"format": "pcm", "sample_rate": 16000}})

        async def run():
            try:
                await s.run()
            finally:
                app["active_sessions"].discard(s)
                if self.session is s:
                    self.session, self.channel = None, None
        asyncio.create_task(run())

    async def _close_session(self):
        if self.channel is not None:
            await self.channel.close()

    def _event(self, name: str, data: dict | None = None):
        from aioesphomeapi import VoiceAssistantEventType
        if not self.connected:
            return
        try:
            self.client.send_voice_assistant_event(getattr(VoiceAssistantEventType, f"VOICE_ASSISTANT_{name}"),
                                                   {k: str(v) for k, v in (data or {}).items()})
        except Exception as e:
            log.debug("evento %s non inviato: %s", name, e)

    async def _voice_start(self, conversation_id: str, flags: int, audio_settings, wake_word: str | None):
        from aioesphomeapi import VoiceAssistantFeature
        if not self.cfg.get("voice"):
            return None
        await self._ensure_session()
        f = self._features()
        streaming = bool(f & VoiceAssistantFeature.SPEAKER and f & VoiceAssistantFeature.API_AUDIO)
        if self.run is not None and self.session is not None:
            await self.session.interrupt()  # nuova domanda mentre rispondeva: si ferma e ascolta
        self.run = {"conversation": conversation_id or secrets.token_hex(8), "streaming": streaming, "vad": False,
                    "vad_end": False, "texts": [], "pcm": [], "tts_started": False, "done": False, "samples": 0}
        self.channel.realtime = streaming
        self._event("RUN_START", {})
        self._event("STT_START", {})
        self.channel.feed_text({"type": "listen", "state": "start", "mode": "auto"})
        log.info("ESPHome '%s': inizia una domanda%s", self.node, f" (parola: {wake_word})" if wake_word else "")
        return 0  # 0 = la voce arriva attraverso l'API (niente porta UDP)

    async def _voice_audio(self, data: bytes, data2: bytes | None):
        run = self.run
        if run is None or self.channel is None:
            return
        if not run["vad"]:
            run["vad"] = True
            self._event("STT_VAD_START", {})
        self.channel.feed_audio(data)
        run["samples"] += len(data) // 2
        s = self.session
        limit = float(self.mgr.cfg.get("no_speech_seconds", 5)) * 16000
        if s is not None and s.listening and not s.vad.started and run["samples"] > limit:
            # nessuno parla (falsa parola di attivazione, aula silenziosa): si chiude invece di ascoltare per sempre
            s.listening = False
            s.buffer.clear()
            s.buffer_samples = 0
            self.on_vad("empty")

    async def _voice_stop(self, server_side: bool):
        run, s = self.run, self.session
        if run is None or s is None:
            return
        if s.listening:  # la scheda ha smesso di mandare la voce prima che finisse la frase: si usa quello che c'è
            s.listening = False
            if s.buffer:
                s.finish_utterance()
            else:
                self.on_vad("empty")
        elif server_side and (s.speaking or (s.turn_task and not s.turn_task.done())) and not run["vad_end"]:
            await s.interrupt()
            self._end_run()

    def on_vad(self, name: str):
        run = self.run
        if run is None:
            return
        if name == "vad_end" and not run["vad_end"]:
            run["vad_end"] = True
            self._event("STT_VAD_END", {})
        elif name == "empty":
            if not run["vad_end"]:
                self._event("STT_VAD_END", {})
            self._event("ERROR", {"code": "stt-no-text-recognized", "message": "Non ho capito, riprova."})
            self._end_run()
        elif name == "room_lost":  # risponde un altro dispositivo della stessa aula: fine del giro in silenzio
            if not run["vad_end"]:
                self._event("STT_VAD_END", {})
            self._end_run()

    async def on_session_message(self, obj: dict):
        run = self.run
        t = obj.get("type")
        if t == "stt":
            if not run["vad_end"]:
                run["vad_end"] = True
                self._event("STT_VAD_END", {})
            self._event("STT_END", {"text": obj.get("text", "")})
            self._event("INTENT_START", {})
        elif t == "tts" and obj.get("state") == "sentence_start":
            run["texts"].append(obj.get("text", ""))
            if run["streaming"] and not run["tts_started"]:
                run["tts_started"] = True
                self._event("INTENT_END", {"conversation_id": run["conversation"]})
                self._event("TTS_START", {"text": obj.get("text", "")})
                self._event("TTS_STREAM_START", {})
        elif t == "tts" and obj.get("state") == "stop":
            await self._finish_tts()

    def on_session_audio(self, pcm_bytes: bytes):
        run = self.run
        pcm = np.frombuffer(pcm_bytes[: len(pcm_bytes) // 2 * 2], dtype=np.int16)
        run["pcm"].append(pcm)
        if run["streaming"] and self.connected:
            rate = self.mgr.app["tts"].sample_rate
            data = resample(pcm, rate, 16000).astype(np.int16).tobytes()
            for i in range(0, len(data), 1024):
                self.client.send_voice_assistant_audio(data[i:i + 1024])

    async def _finish_tts(self):
        run = self.run
        if run is None or run["done"]:
            return
        text = " ".join(run["texts"]).strip()
        url = ""
        if run["pcm"]:
            url = self.mgr.store_audio(np.concatenate(run["pcm"]), self.mgr.app["tts"].sample_rate)
        if run["streaming"]:
            if run["tts_started"]:
                self._event("TTS_END", {"url": url})
                self._event("TTS_STREAM_END", {})
        elif text:
            self._event("INTENT_END", {"conversation_id": run["conversation"]})
            self._event("TTS_START", {"text": text})
            self._event("TTS_END", {"url": url})
        self._end_run()

    def _end_run(self):
        run = self.run
        if run is None or run["done"]:
            return
        run["done"] = True
        self._event("RUN_END", {})
        self.run = None
        if self.session is not None:
            self.session.listening = False  # fino alla prossima parola di attivazione

    async def announce(self, text: str) -> bool:
        """Avviso a voce sulle schede che lo permettono (funzione "announce" di ESPHome)."""
        from aioesphomeapi import VoiceAssistantFeature
        if not (self.connected and self._features() & VoiceAssistantFeature.ANNOUNCE):
            return False
        pcm = await self.mgr.app["tts"].synthesize(text)
        url = self.mgr.store_audio(pcm, self.mgr.app["tts"].sample_rate)
        try:
            await self.client.send_voice_assistant_announcement_await_response(url, 60, text=text)
            return True
        except Exception as e:
            log.warning("ESPHome '%s': avviso non riprodotto (%s)", self.node, e)
            return False


class EsphomeManager:
    def __init__(self, app, data_dir: Path, box=None, cfg: dict | None = None):
        self.app = app
        self.box = box
        self.cfg = cfg or {}
        self.store = JsonStore(Path(data_dir) / "esphome.json")
        self.store.data.setdefault("discovery", False)
        self.store.data.setdefault("devices", [])
        self.devices: dict[str, EspDevice] = {}
        self.found: dict[str, dict] = {}   # trovati in rete (mDNS), per nome
        self.audio: dict[str, tuple[bytes, float]] = {}
        self._azc = None
        self._browser = None

    def zeroconf(self):
        return self._azc.zeroconf if self._azc else None

    # ------------------------------------------------------------------ avvio
    async def start(self):
        if self.store.data["discovery"] or any(d["host"].endswith(".local") for d in self.store.data["devices"]):
            await self._start_zeroconf()
        for d in self.store.data["devices"]:
            if d.get("enabled", True):
                await self._start_device(d)

    async def stop(self):
        for dev in list(self.devices.values()):
            await dev.stop()
        self.devices.clear()
        await self._stop_zeroconf()

    async def _start_zeroconf(self):
        if self._azc is not None:
            return
        try:
            from zeroconf.asyncio import AsyncZeroconf
            self._azc = AsyncZeroconf()
        except Exception as e:
            log.warning("Scoperta mDNS non disponibile: %s", e)
            return
        if self.store.data["discovery"]:
            self._browse()

    def _browse(self):
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser

        def on_change(zeroconf, service_type, name, state_change):
            if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                asyncio.ensure_future(self._resolve(service_type, name))
            elif state_change == ServiceStateChange.Removed:
                self.found.pop(name.split(".")[0], None)
        self._browser = AsyncServiceBrowser(self._azc.zeroconf, SERVICE, handlers=[on_change])

    async def _resolve(self, service_type: str, name: str):
        from zeroconf.asyncio import AsyncServiceInfo
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self._azc.zeroconf, 3000):
            return
        props = {k.decode(errors="replace"): (v or b"").decode(errors="replace") for k, v in (info.properties or {}).items()}
        node = name.split(".")[0]
        addrs = info.parsed_addresses()
        if len(self.found) < 200:
            self.found[node] = {"node": node, "host": addrs[0] if addrs else f"{node}.local", "port": info.port,
                                "mac": props.get("mac", ""), "model": props.get("board", ""),
                                "friendly_name": props.get("friendly_name", ""), "version": props.get("version", ""),
                                "time": time.time()}

    async def _stop_zeroconf(self):
        if self._browser is not None:
            await self._browser.async_cancel()
            self._browser = None
        if self._azc is not None:
            await self._azc.async_close()
            self._azc = None

    async def set_discovery(self, on: bool):
        with self.store.lock:
            self.store.data["discovery"] = bool(on)
            self.store.save()
        if on:
            if self._azc is None:
                await self._start_zeroconf()
            elif self._browser is None:
                self._browse()
        else:
            self.found.clear()
            if self._browser is not None:
                await self._browser.async_cancel()
                self._browser = None

    async def _start_device(self, d: dict):
        dev = EspDevice(self, d)
        self.devices[d["id"]] = dev
        try:
            await dev.start()
        except Exception as e:
            dev.last_error = str(e)
            log.warning("ESPHome '%s': %s", d["name"], e)

    # ------------------------------------------------------------------ dispositivi approvati
    def device_cfg(self, did: str) -> dict | None:
        return next((d for d in self.store.data["devices"] if d["id"] == did), None)

    async def save_device(self, d: dict) -> dict:
        did = str(d.get("id") or secrets.token_hex(4))
        old = self.device_cfg(did) or {}
        host = str(d.get("host", "")).strip()
        if not HOST_RE.match(host):
            raise ValueError("Indirizzo non valido (IP o nome.local)")
        try:
            port = int(d.get("port") or 6053)
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            raise ValueError("Porta non valida") from None
        name = _safe(str(d.get("name") or host.split(".")[0]))
        if any(x["name"] == name and x["id"] != did for x in self.store.data["devices"]):
            raise ValueError("C'è già un dispositivo con questo nome")
        enc = (lambda v: self.box.encrypt(v) if self.box else v)
        key = str(d.get("key") or "").strip()
        if key and key != "••••••":
            try:
                if len(base64.b64decode(key, validate=True)) != 32:
                    raise ValueError
            except ValueError:
                raise ValueError("Chiave di cifratura non valida: è il testo di 44 caratteri dopo "
                                 "\"api: encryption: key:\" nel file YAML") from None
        pw = str(d.get("password") or "")
        dev = {"id": did, "name": name, "label": str(d.get("label") or "")[:60], "host": host, "port": port,
               "node": str(d.get("node") or "")[:60], "voice": bool(d.get("voice")), "enabled": True,
               "key": old.get("key", "") if key == "••••••" else (enc(key) if key else ""),
               "password": old.get("password", "") if pw == "••••••" else (enc(pw) if pw else "")}
        with self.store.lock:
            self.store.data["devices"] = [x for x in self.store.data["devices"] if x["id"] != did] + [dev]
            self.store.save()
        if did in self.devices:
            await self.devices.pop(did).stop()
        if host.endswith(".local"):
            await self._start_zeroconf()
        await self._start_device(dev)
        return dev

    async def delete_device(self, did: str) -> bool:
        before = len(self.store.data["devices"])
        with self.store.lock:
            self.store.data["devices"] = [x for x in self.store.data["devices"] if x["id"] != did]
            self.store.save()
        if did in self.devices:
            await self.devices.pop(did).stop()
        return len(self.store.data["devices"]) < before

    # ------------------------------------------------------------------ per Session, avvisi e pagina Admin
    def toolset_for(self, role: str) -> ToolSet:
        roles = self.cfg.get("command_roles", COMMAND_ROLES)
        ts = ToolSet()
        for dev in self.devices.values():
            ts.merge(dev.toolset(role, roles))
        return ts

    def store_audio(self, pcm: np.ndarray, rate: int) -> str:
        now = time.time()
        for k in [k for k, (_, exp) in self.audio.items() if exp < now]:
            del self.audio[k]
        aid = secrets.token_urlsafe(16)
        self.audio[aid] = (make_wav(pcm, rate), now + AUDIO_TTL)
        return f"{self.app['http_base']}/xiaozhi/esphome/{aid}.wav"

    def get_audio(self, aid: str) -> bytes | None:
        item = self.audio.get(aid)
        return item[0] if item and item[1] > time.time() else None

    async def announce(self, text: str, target: str = "all") -> list[str]:
        done = []
        for dev in self.devices.values():
            if target in ("all", f"esphome:{dev.node}") and await dev.announce(text):
                done.append(f"esphome:{dev.node}")
        return done

    def online(self) -> list[str]:
        return [f"esphome:{d.node}" for d in self.devices.values() if d.connected]

    def status(self) -> dict:
        from aioesphomeapi import VoiceAssistantFeature
        devs = []
        for d in self.store.data["devices"]:
            dev = self.devices.get(d["id"])
            info = dev.info if dev else None
            ents = []
            if dev and dev.connected:
                for key, e in dev.entities.items():
                    st = dev.states.get(key)
                    ents.append({"kind": dev._kind(e), "name": e.name, "value": dev._value(e, st[0]) if st else None})
            flags = (info.voice_assistant_feature_flags or 0) if info else 0
            devs.append({**{k: v for k, v in d.items() if k not in ("key", "password")},
                         "has_key": bool(d.get("key")), "has_password": bool(d.get("password")),
                         "connected": bool(dev and dev.connected), "error": dev.last_error if dev else "",
                         "model": info.model if info else "", "version": info.esphome_version if info else "",
                         "voice_capable": bool(flags & VoiceAssistantFeature.VOICE_ASSISTANT),
                         "entities": ents})
        approved = {(d["host"], d["port"]) for d in self.store.data["devices"]} | {d.get("node") for d in self.store.data["devices"]}
        found = [f for f in self.found.values() if (f["host"], f["port"]) not in approved and f["node"] not in approved]
        return {"discovery": bool(self.store.data["discovery"]), "devices": devs,
                "found": sorted(found, key=lambda f: f["node"]), "mdns": self._azc is not None}
