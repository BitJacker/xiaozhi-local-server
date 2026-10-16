"""Protocollo MQTT 3.1.1 (la parte che serve): lettura e scrittura dei pacchetti, confronto degli argomenti.

Usato sia dal broker integrato (mqtt_broker.py) sia dal client per i broker esterni (mqtt_client.py).
Specifica: OASIS MQTT Version 3.1.1. Livelli di qualità supportati: QoS 0 e 1 (QoS 2 viene trattato come 1).
"""
from __future__ import annotations

import asyncio
import struct

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14
MAX_PACKET = 1 << 20  # 1 MB: niente pacchetti enormi per riempire la memoria


class MqttError(Exception):
    pass


# ------------------------------------------------------------------ codifica di base
def encode_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def string(s: str | bytes) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return struct.pack(">H", len(b)) + b


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + encode_length(len(body)) + body


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    """Ritorna (tipo, flag, corpo). Solleva IncompleteReadError a connessione chiusa."""
    first = (await reader.readexactly(1))[0]
    mult, length = 1, 0
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    else:
        raise MqttError("lunghezza non valida")
    if length > MAX_PACKET:
        raise MqttError("pacchetto troppo grande")
    body = await reader.readexactly(length) if length else b""
    return first >> 4, first & 0x0F, body


class Buf:
    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def u8(self) -> int:
        self.i += 1
        return self.d[self.i - 1]

    def u16(self) -> int:
        self.i += 2
        return struct.unpack(">H", self.d[self.i - 2:self.i])[0]

    def raw(self) -> bytes:
        n = self.u16()
        self.i += n
        if self.i > len(self.d):
            raise MqttError("pacchetto troncato")
        return self.d[self.i - n:self.i]

    def str(self) -> str:
        return self.raw().decode("utf-8", "replace")

    def rest(self) -> bytes:
        return self.d[self.i:]

    def more(self) -> bool:
        return self.i < len(self.d)


# ------------------------------------------------------------------ pacchetti
def connect(client_id: str, username: str | None, password: str | None, keepalive: int = 60,
            clean: bool = True) -> bytes:
    flags = (0x02 if clean else 0) | (0x80 if username else 0) | (0x40 if password else 0)
    body = string("MQTT") + bytes([4, flags]) + struct.pack(">H", keepalive) + string(client_id)
    if username:
        body += string(username)
    if password:
        body += string(password)
    return packet(CONNECT, 0, body)


def parse_connect(body: bytes) -> dict:
    b = Buf(body)
    proto, level, flags, keepalive = b.str(), b.u8(), b.u8(), b.u16()
    if proto not in ("MQTT", "MQIsdp"):
        raise MqttError("protocollo sconosciuto")
    out = {"protocol": proto, "level": level, "keepalive": keepalive, "clean": bool(flags & 0x02),
           "client_id": b.str(), "username": None, "password": None, "will": None}
    if flags & 0x04:  # messaggio "testamento": lo leggiamo ma non lo usiamo
        out["will"] = (b.str(), b.raw())
    if flags & 0x80:
        out["username"] = b.str()
    if flags & 0x40:
        out["password"] = b.raw().decode("utf-8", "replace")
    return out


def connack(code: int = 0, session_present: bool = False) -> bytes:
    return packet(CONNACK, 0, bytes([1 if session_present else 0, code]))


def publish(topic: str, payload: bytes | str, qos: int = 0, retain: bool = False, packet_id: int = 0) -> bytes:
    p = payload.encode() if isinstance(payload, str) else payload
    body = string(topic) + (struct.pack(">H", packet_id) if qos else b"") + p
    return packet(PUBLISH, (qos << 1) | (1 if retain else 0), body)


def parse_publish(flags: int, body: bytes) -> dict:
    b = Buf(body)
    qos = (flags >> 1) & 0x03
    topic = b.str()
    pid = b.u16() if qos else 0
    return {"topic": topic, "qos": qos, "retain": bool(flags & 1), "dup": bool(flags & 8), "packet_id": pid,
            "payload": b.rest()}


def puback(packet_id: int) -> bytes:
    return packet(PUBACK, 0, struct.pack(">H", packet_id))


def subscribe(packet_id: int, topics: list[tuple[str, int]]) -> bytes:
    body = struct.pack(">H", packet_id) + b"".join(string(t) + bytes([q]) for t, q in topics)
    return packet(SUBSCRIBE, 2, body)


def parse_subscribe(body: bytes) -> tuple[int, list[tuple[str, int]]]:
    b = Buf(body)
    pid = b.u16()
    topics = []
    while b.more():
        topics.append((b.str(), b.u8() & 0x03))
    return pid, topics


def suback(packet_id: int, codes: list[int]) -> bytes:
    return packet(SUBACK, 0, struct.pack(">H", packet_id) + bytes(codes))


def parse_unsubscribe(body: bytes) -> tuple[int, list[str]]:
    b = Buf(body)
    pid = b.u16()
    topics = []
    while b.more():
        topics.append(b.str())
    return pid, topics


def unsuback(packet_id: int) -> bytes:
    return packet(UNSUBACK, 0, struct.pack(">H", packet_id))


PINGREQ_PKT = packet(PINGREQ, 0, b"")
PINGRESP_PKT = packet(PINGRESP, 0, b"")
DISCONNECT_PKT = packet(DISCONNECT, 0, b"")


# ------------------------------------------------------------------ argomenti
def valid_filter(f: str) -> bool:
    if not f or len(f) > 512:
        return False
    parts = f.split("/")
    for i, p in enumerate(parts):
        if "#" in p and (p != "#" or i != len(parts) - 1):
            return False
        if "+" in p and p != "+":
            return False
    return True


def topic_matches(flt: str, topic: str) -> bool:
    """Un filtro con + (un livello) e # (tutti i livelli successivi) corrisponde all'argomento?"""
    if topic.startswith("$") and flt[:1] in ("+", "#"):
        return False  # gli argomenti di sistema non vengono presi dai caratteri jolly
    fp, tp = flt.split("/"), topic.split("/")
    for i, part in enumerate(fp):
        if part == "#":
            return True
        if i >= len(tp):
            return False
        if part != "+" and part != tp[i]:
            return False
    return len(fp) == len(tp)
