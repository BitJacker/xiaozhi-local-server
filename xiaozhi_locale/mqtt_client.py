"""Client MQTT per collegarsi a un broker esterno (es. The Things Network o ChirpStack per i sensori LoRaWAN,
oppure un Mosquitto già presente a scuola). Si ricollega da solo e ricorda l'ultimo valore di ogni argomento."""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Awaitable, Callable

from . import mqtt_proto as mp

log = logging.getLogger("mqtt")


class MqttClient:
    def __init__(self, host: str, port: int = 1883, tls: bool = False, username: str | None = None,
                 password: str | None = None, client_id: str = "husky-locale", topics: list[str] | None = None,
                 on_message: Callable[[str, bytes], Awaitable[None]] | None = None, keepalive: int = 60):
        self.host, self.port, self.tls = host, port, tls
        self.username, self.password, self.client_id = username, password, client_id
        self.topics = topics or []
        self.on_message = on_message
        self.keepalive = keepalive
        self.connected = False
        self.last_error = ""
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._pid = 0

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._writer:
            try:
                self._writer.write(mp.DISCONNECT_PKT)
                self._writer.close()
            except Exception:
                pass
        self.connected = False

    def _next_pid(self) -> int:
        self._pid = self._pid % 65535 + 1
        return self._pid

    async def publish(self, topic: str, payload: bytes | str, retain: bool = False) -> bool:
        if not self.connected or not self._writer:
            return False
        self._writer.write(mp.publish(topic, payload, 0, retain))
        await self._writer.drain()
        return True

    async def _run(self):
        delay = 1
        while True:
            try:
                ctx = ssl.create_default_context() if self.tls else None
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=ctx), timeout=10)
                self._writer = writer
                writer.write(mp.connect(self.client_id, self.username, self.password, self.keepalive))
                await writer.drain()
                ptype, _, body = await asyncio.wait_for(mp.read_packet(reader), timeout=10)
                if ptype != mp.CONNACK or len(body) < 2 or body[1] != 0:
                    code = body[1] if len(body) > 1 else -1
                    raise mp.MqttError({4: "nome utente o password sbagliati", 5: "non autorizzato"}.get(code, f"rifiutato ({code})"))
                if self.topics:
                    writer.write(mp.subscribe(self._next_pid(), [(t, 0) for t in self.topics]))
                    await writer.drain()
                self.connected, self.last_error, delay = True, "", 1
                log.info("MQTT esterno %s:%d collegato", self.host, self.port)
                last_ping = time.monotonic()
                while True:
                    try:
                        ptype, flags, body = await asyncio.wait_for(mp.read_packet(reader), timeout=self.keepalive / 2)
                    except asyncio.TimeoutError:
                        ptype = None
                    if ptype == mp.PUBLISH:
                        msg = mp.parse_publish(flags, body)
                        if msg["qos"]:
                            writer.write(mp.puback(msg["packet_id"]))
                        if self.on_message:
                            await self.on_message(msg["topic"], msg["payload"])
                    if time.monotonic() - last_ping > self.keepalive / 2:
                        writer.write(mp.PINGREQ_PKT)
                        await writer.drain()
                        last_ping = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.last_error = str(e) or type(e).__name__
                log.warning("MQTT esterno %s:%d non raggiungibile: %s. Riprovo tra %d s", self.host, self.port,
                            self.last_error, delay)
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
