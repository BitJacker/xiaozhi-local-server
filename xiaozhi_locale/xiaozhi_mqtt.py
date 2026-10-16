"""Protocollo xiaozhi MQTT+UDP: comandi via MQTT (broker integrato), voce via UDP cifrata AES-128-CTR.

Come funziona (dal protocollo pubblico del firmware xiaozhi-esp32):
  * il dispositivo si collega al broker con client_id "GID@@@mac_con_trattini_bassi@@@client-id" e come
    password il suo token (lo riceve dall'OTA solo se è approvato);
  * scrive i messaggi JSON su "device-server"; NON si iscrive a niente: il server gli manda le risposte
    direttamente sulla sua connessione, sull'argomento "devices/p2p/<mac>";
  * con "hello" (transport "udp") chiede il canale audio: la risposta contiene indirizzo e porta UDP, chiave e
    nonce AES. Ogni pacchetto UDP ha un'intestazione di 16 byte |tipo 1|flag|lunghezza|ssrc|timestamp|sequenza|
    che è anche il contatore iniziale di AES-CTR; il resto è audio Opus cifrato.
Per il resto la conversazione è identica a quella via WebSocket: se ne occupa la stessa Session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import time

from aiohttp import WSMsgType
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .mqtt_broker import Broker, ClientConn
from .session import Session

log = logging.getLogger("xiaozhi_mqtt")
HEADER = struct.Struct(">BBHIII")  # tipo, flag, lunghezza, ssrc, timestamp, sequenza


def aes_ctr(key: bytes, nonce: bytes, data: bytes) -> bytes:
    c = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return c.update(data) + c.finalize()


class _Msg:
    def __init__(self, type_, data):
        self.type, self.data = type_, data


class FakeRequest:
    """Quello che la Session legge dalla richiesta WebSocket: intestazioni, query, indirizzo."""

    def __init__(self, mac: str, uuid: str, ip: str):
        self.headers = {"Device-Id": mac, "Client-Id": uuid, "Protocol-Version": "1"}
        self.query = {}
        self.remote = ip


class UdpChannel:
    """Si comporta come il WebSocket di aiohttp per la Session: messaggi in arrivo in coda, risposte via
    MQTT (testo) e via UDP cifrato (audio)."""

    def __init__(self, gw: "XiaozhiGateway", conn: ClientConn, mac: str, uuid: str):
        self.gw, self.conn, self.mac, self.uuid = gw, conn, mac, uuid
        self.reply_topic = "devices/p2p/" + mac.replace(":", "_")
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.ssrc = secrets.randbits(32)
        self.key = secrets.token_bytes(16)
        self.nonce = HEADER.pack(1, 0, 0, self.ssrc, 0, 0)
        self.remote = None          # indirizzo UDP del dispositivo (lo impariamo dal primo pacchetto)
        self.remote_seq = 0
        self.local_seq = 0
        self.t0 = time.monotonic()
        self.session_id = ""

    # ---- interfaccia "WebSocket" usata dalla Session
    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def send_str(self, text: str):
        if self.closed:
            return
        try:
            obj = json.loads(text)
        except ValueError:
            return
        if obj.get("type") == "hello":  # la risposta al hello porta i dati del canale UDP
            self.session_id = obj.get("session_id", "")
            obj.update(transport="udp", version=3, udp={
                "server": self.gw.public_host, "port": self.gw.udp_port, "encryption": "aes-128-ctr",
                "key": self.key.hex(), "nonce": self.nonce.hex()})
        await self.gw.broker.send_direct(self.conn.client_id, self.reply_topic, json.dumps(obj, ensure_ascii=False))

    async def send_bytes(self, opus: bytes):
        if self.closed or self.remote is None or self.gw.transport is None:
            return  # il dispositivo non ha ancora aperto il canale audio
        self.local_seq = (self.local_seq + 1) & 0xFFFFFFFF
        ts = int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF
        header = HEADER.pack(1, 0, len(opus), self.ssrc, ts, self.local_seq)
        self.gw.transport.sendto(header + aes_ctr(self.key, header, opus), self.remote)

    async def close(self, code: int = 1000, message: bytes = b""):
        if self.closed:
            return
        self.closed = True
        await self.queue.put(None)

    # ---- dal gateway
    def feed_text(self, obj: dict):
        self.queue.put_nowait(_Msg(WSMsgType.TEXT, json.dumps(obj)))

    def feed_udp(self, data: bytes, addr) -> bool:
        _type, _flags, length, ssrc, _ts, seq = HEADER.unpack_from(data)
        if len(data) != 16 + length or seq <= self.remote_seq:
            return False  # pacchetto troncato, vecchio o ripetuto (anti-replay)
        self.remote_seq, self.remote = seq, addr
        self.queue.put_nowait(_Msg(WSMsgType.BINARY, aes_ctr(self.key, data[:16], data[16:])))
        return True


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, gw: "XiaozhiGateway"):
        self.gw = gw

    def datagram_received(self, data, addr):
        if len(data) < 16 or data[0] != 1:
            return
        ssrc = struct.unpack_from(">I", data, 4)[0]
        ch = self.gw.by_ssrc.get(ssrc)
        if ch and not ch.closed:
            ch.feed_udp(data, addr)


class XiaozhiGateway:
    def __init__(self, app, cfg: dict):
        self.app = app
        self.cfg = cfg or {}
        self.udp_port = int(self.cfg.get("udp_port", 8884))
        self.public_host = self.cfg.get("public_host") or app.get("lan_ip") or "127.0.0.1"
        self.broker: Broker | None = None
        self.transport = None
        self.channels: dict[str, UdpChannel] = {}  # per client_id
        self.by_ssrc: dict[int, UdpChannel] = {}

    def attach(self, broker: Broker):
        self.broker = broker
        broker.listeners.append(self._on_message)
        broker.disconnect_listeners.append(self._on_disconnect)

    async def start(self):
        loop = asyncio.get_running_loop()
        try:
            self.transport, _ = await loop.create_datagram_endpoint(lambda: _Udp(self),
                                                                    local_addr=(self.cfg.get("host", "0.0.0.0"), self.udp_port))
            self.udp_port = self.transport.get_extra_info("sockname")[1]
            log.info("Audio UDP per i dispositivi xiaozhi sulla porta %d", self.udp_port)
        except OSError as e:
            log.error("Porta UDP %d non disponibile: %s", self.udp_port, e)

    async def stop(self):
        for ch in list(self.channels.values()):
            await ch.close()
        if self.transport:
            self.transport.close()
            self.transport = None

    async def _on_disconnect(self, conn: ClientConn):
        ch = self.channels.pop(conn.client_id, None)
        if ch:
            await ch.close()

    async def _on_message(self, source: ClientConn | None, topic: str, payload: bytes, retain: bool):
        if source is None or source.info.get("kind") != "xiaozhi" or topic != "device-server":
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        t = obj.get("type")
        if t == "hello":
            await self._open(source, obj)
        elif t == "goodbye":
            ch = self.channels.pop(source.client_id, None)
            if ch:
                await ch.close()
        else:
            ch = self.channels.get(source.client_id)
            if ch:
                ch.feed_text(obj)
            elif t != "goodbye":  # nessun canale aperto: come il firmware si aspetta, si chiude la sessione
                await self.broker.send_direct(source.client_id, "devices/p2p/" + source.info["mac"].replace(":", "_"),
                                              json.dumps({"type": "goodbye", "session_id": obj.get("session_id", "")}))

    async def _open(self, conn: ClientConn, hello: dict):
        old = self.channels.pop(conn.client_id, None)
        if old:
            await old.close()
        mac, uuid = conn.info["mac"], conn.info["uuid"]
        auth = self.app["auth"]
        ch = UdpChannel(self, conn, mac, uuid)
        self.channels[conn.client_id] = ch
        self.by_ssrc[ch.ssrc] = ch
        token = auth.device_token(mac, uuid)

        def identity():
            dev = auth.device_ok(mac, uuid, token)
            return {"username": dev.get("name") or mac, "role": "device", "token": token} if dev else None

        ident = identity()
        if ident is None:
            await ch.close()
            return
        session = Session(self.app, ch, FakeRequest(mac, uuid, conn.ip), user=ident["username"], role="device",
                          device_token=token, identity_check=identity)
        self.app["active_sessions"].add(session)
        hello = {**hello, "audio_params": hello.get("audio_params") or {"format": "opus", "sample_rate": 16000}}
        ch.feed_text(hello)

        async def run():
            try:
                await session.run()
            finally:
                self.app["active_sessions"].discard(session)
                self.by_ssrc.pop(ch.ssrc, None)
                if self.channels.get(conn.client_id) is ch:
                    del self.channels[conn.client_id]
                if not conn.closed:  # fine della conversazione decisa dal server: il dispositivo chiude l'audio
                    await self.broker.send_direct(conn.client_id, ch.reply_topic,
                                                  json.dumps({"type": "goodbye", "session_id": ch.session_id}))
        asyncio.create_task(run())

    # ------------------------------------------------------------------ messaggi senza conversazione
    async def send_to_device(self, mac: str, obj: dict) -> bool:
        """Messaggio di controllo a un dispositivo collegato via MQTT (es. avviso, notifica vocale)."""
        if not self.broker:
            return False
        for c in self.broker.clients.values():
            if c.info.get("kind") == "xiaozhi" and c.info.get("mac", "").lower() == mac.lower():
                return await self.broker.send_direct(c.client_id, "devices/p2p/" + c.info["mac"].replace(":", "_"),
                                                     json.dumps(obj, ensure_ascii=False))
        return False

    def online_devices(self) -> list[str]:
        if not self.broker:
            return []
        return [c.info["mac"] for c in self.broker.clients.values() if c.info.get("kind") == "xiaozhi"]
