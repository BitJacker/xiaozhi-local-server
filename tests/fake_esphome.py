"""Finto dispositivo ESPHome per i test: parla l'API nativa (in chiaro) come il firmware vero.

Ha un sensore di temperatura, un interruttore, una luce, un pulsante e un numero; se richiesto anche l'assistente
vocale (voice_assistant) con altoparlante in streaming o con media player (file WAV) e gli annunci.
"""
from __future__ import annotations

import asyncio

from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

TYPE_OF = {v: k for k, v in MESSAGE_TYPE_TO_PROTO.items()}
VA, SPEAKER, API_AUDIO, ANNOUNCE = 1, 2, 4, 16
EVENT_NAMES = {0: "ERROR", 1: "RUN_START", 2: "RUN_END", 3: "STT_START", 4: "STT_END", 5: "INTENT_START",
               6: "INTENT_END", 7: "TTS_START", 8: "TTS_END", 9: "WAKE_WORD_START", 10: "WAKE_WORD_END",
               11: "STT_VAD_START", 12: "STT_VAD_END", 98: "TTS_STREAM_START", 99: "TTS_STREAM_END",
               100: "INTENT_PROGRESS"}


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


async def _read_varint(reader) -> int:
    shift = result = 0
    while True:
        b = (await reader.readexactly(1))[0]
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result
        shift += 7


class FakeEsphome:
    def __init__(self, name: str, features: int = 0):
        self.name, self.features = name, features
        self.port = 0
        self.writer = None
        self.connected = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.switch = False
        self.light = (False, 0.0)
        self.pressed = 0
        self.number = 0.0
        self.va_subscribed = asyncio.Event()
        self.va_response: asyncio.Future | None = None
        self.events: list[tuple[str, dict]] = []
        self.new_event = asyncio.Event()
        self.audio_in = bytearray()
        self.announces: list[pb.VoiceAssistantAnnounceRequest] = []
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.writer:
            self.writer.close()
        if self._server:
            self._server.close()

    def send(self, msg):
        data = msg.SerializeToString()
        self.writer.write(b"\x00" + _varint(len(data)) + _varint(TYPE_OF[type(msg)]) + data)

    async def _client(self, reader, writer):
        if self.writer is not None:  # una connessione alla volta, come il dispositivo vero
            writer.close()
            return
        self.writer = writer
        self.disconnected.clear()
        try:
            while True:
                if (await reader.readexactly(1)) != b"\x00":
                    break  # cifratura richiesta dal client: questo finto dispositivo è solo in chiaro
                size = await _read_varint(reader)
                mtype = await _read_varint(reader)
                data = await reader.readexactly(size) if size else b""
                cls = MESSAGE_TYPE_TO_PROTO.get(mtype)
                if cls is None:
                    continue
                msg = cls()
                msg.ParseFromString(data)
                if await self._handle(msg) is False:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writer = None
            self.connected.clear()
            self.disconnected.set()
            writer.close()

    async def _handle(self, m):
        t = type(m)
        if t is pb.HelloRequest:
            self.send(pb.HelloResponse(api_version_major=1, api_version_minor=13, server_info="finto", name=self.name))
            self.connected.set()
        elif t is pb.AuthenticationRequest:
            self.send(pb.AuthenticationResponse(invalid_password=False))
        elif t is pb.DisconnectRequest:
            self.send(pb.DisconnectResponse())
            return False
        elif t is pb.PingRequest:
            self.send(pb.PingResponse())
        elif t is pb.DeviceInfoRequest:
            self.send(pb.DeviceInfoResponse(name=self.name, friendly_name=f"Finto {self.name}", mac_address="AA:BB:CC:00:00:01",
                                            esphome_version="2026.9.0", model="esp32-s3-box", manufacturer="Espressif",
                                            voice_assistant_feature_flags=self.features))
        elif t is pb.ListEntitiesRequest:
            self.send(pb.ListEntitiesSensorResponse(object_id="temperatura", key=1, name="Temperatura",
                                                    unit_of_measurement="°C", accuracy_decimals=1))
            self.send(pb.ListEntitiesSwitchResponse(object_id="ventola", key=2, name="Ventola"))
            self.send(pb.ListEntitiesLightResponse(object_id="luce", key=3, name="Luce", supported_color_modes=[3]))
            self.send(pb.ListEntitiesButtonResponse(object_id="campanella", key=4, name="Campanella"))
            self.send(pb.ListEntitiesNumberResponse(object_id="soglia", key=5, name="Soglia", min_value=0, max_value=50,
                                                    step=1))
            self.send(pb.ListEntitiesDoneResponse())
        elif t is pb.SubscribeStatesRequest:
            self.send(pb.SensorStateResponse(key=1, state=21.5))
            self._send_states()
        elif t is pb.SwitchCommandRequest:
            self.switch = m.state
            self._send_states()
        elif t is pb.LightCommandRequest:
            self.light = (m.state if m.has_state else self.light[0], m.brightness if m.has_brightness else self.light[1])
            self._send_states()
        elif t is pb.ButtonCommandRequest:
            self.pressed += 1
        elif t is pb.NumberCommandRequest:
            self.number = m.state
            self.send(pb.NumberStateResponse(key=5, state=self.number))
        elif t is pb.SubscribeVoiceAssistantRequest:
            if m.subscribe:
                self.va_subscribed.set()
        elif t is pb.VoiceAssistantResponse:
            if self.va_response and not self.va_response.done():
                self.va_response.set_result(m)
        elif t is pb.VoiceAssistantEventResponse:
            self.events.append((EVENT_NAMES.get(m.event_type, str(m.event_type)), {d.name: d.value for d in m.data}))
            self.new_event.set()
        elif t is pb.VoiceAssistantAudio:
            self.audio_in += m.data
        elif t is pb.VoiceAssistantAnnounceRequest:
            self.announces.append(m)
            self.send(pb.VoiceAssistantAnnounceFinished(success=True))

    def _send_states(self):
        self.send(pb.SwitchStateResponse(key=2, state=self.switch))
        self.send(pb.LightStateResponse(key=3, state=self.light[0], brightness=self.light[1], color_mode=3))
        self.send(pb.NumberStateResponse(key=5, state=self.number))

    async def wait_event(self, name: str, timeout: float = 60) -> dict | None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            for n, d in self.events:
                if n == name:
                    return d
            self.new_event.clear()
            try:
                await asyncio.wait_for(self.new_event.wait(), max(0.05, end - loop.time()))
            except asyncio.TimeoutError:
                break
        return None

    async def ask(self, pcm16k: bytes, chunk: int = 1024) -> pb.VoiceAssistantResponse:
        """Come dopo la parola di attivazione: chiede di iniziare e manda la voce finché il server dice basta."""
        self.events.clear()
        self.audio_in = bytearray()
        self.va_response = asyncio.get_running_loop().create_future()
        self.send(pb.VoiceAssistantRequest(start=True, conversation_id="", flags=1, wake_word_phrase="Ehi Husky"))
        resp = await asyncio.wait_for(self.va_response, 10)
        if resp.error:
            return resp
        for i in range(0, len(pcm16k), chunk):
            if any(n in ("STT_VAD_END", "ERROR", "RUN_END") for n, _ in self.events):
                break  # il server ha sentito la fine della frase: la scheda smette di mandare audio
            self.send(pb.VoiceAssistantAudio(data=pcm16k[i:i + chunk]))
            await asyncio.sleep(chunk / 2 / 16000 / 4)  # 4 volte più veloce del tempo reale
        return resp
