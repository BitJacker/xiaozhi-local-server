"""Broker MQTT integrato: ci si collegano sensori e attuatori (ESP8266, ESP32, ESPHome, gateway LoRaWAN locali)
e i dispositivi xiaozhi che usano il protocollo MQTT+UDP.

Chi si collega deve avere un account (creato nella pagina Admin) oppure essere un dispositivo xiaozhi approvato.
Ogni account ha argomenti in cui può scrivere e leggere. Limiti contro abusi: connessioni totali e per indirizzo,
messaggi al secondo, dimensione dei pacchetti. Consegna con QoS 0 (la rete è locale); i messaggi QoS 1 ricevuti
vengono confermati.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable

from . import mqtt_proto as mp

log = logging.getLogger("mqtt")

# risultato dell'autenticazione: None = rifiutato, altrimenti {"kind": ..., "pub": [filtri], "sub": [filtri], ...}
AuthFn = Callable[[str, str | None, str | None, str], Awaitable[dict | None]]


class ClientConn:
    def __init__(self, broker: "Broker", reader, writer, peer: str):
        self.broker, self.reader, self.writer, self.peer = broker, reader, writer, peer
        self.client_id = ""
        self.username: str | None = None
        self.info: dict = {}
        self.subs: dict[str, int] = {}
        self.connected = False
        self.closed = False
        self._lock = asyncio.Lock()
        self._msg_times: list[float] = []

    @property
    def ip(self) -> str:
        return self.peer.split(":")[0] if self.peer.count(":") == 1 else self.peer.rsplit(":", 1)[0]

    async def write(self, data: bytes):
        if self.closed:
            return
        async with self._lock:
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (ConnectionError, OSError):
                await self.close()

    async def send(self, topic: str, payload: bytes | str, retain: bool = False):
        await self.write(mp.publish(topic, payload, 0, retain))

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass

    def may(self, kind: str, topic: str) -> bool:
        return any(mp.topic_matches(f, topic) for f in self.info.get(kind, []))

    def rate_ok(self, per_second: int) -> bool:
        now = time.monotonic()
        self._msg_times = [t for t in self._msg_times if now - t < 1.0]
        self._msg_times.append(now)
        return len(self._msg_times) <= per_second


class Broker:
    def __init__(self, authenticate: AuthFn, max_clients: int = 200, max_per_ip: int = 30, msgs_per_second: int = 50):
        self.authenticate = authenticate
        self.max_clients, self.max_per_ip, self.msgs_per_second = max_clients, max_per_ip, msgs_per_second
        self.clients: dict[str, ClientConn] = {}
        self.retained: dict[str, bytes] = {}
        # chiamati per ogni messaggio pubblicato (ponte sensori, ESPHome, xiaozhi) e alla disconnessione
        self.listeners: list[Callable[[ClientConn | None, str, bytes, bool], Awaitable[None]]] = []
        self.disconnect_listeners: list[Callable[[ClientConn], Awaitable[None]]] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self, host: str = "0.0.0.0", port: int = 1883):
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("Broker MQTT integrato in ascolto sulla porta %d", self.port)

    async def stop(self):
        if self._server:
            self._server.close()
        for c in list(self.clients.values()):
            await c.close()
        self.clients.clear()

    # ------------------------------------------------------------------ pubblicazione
    async def publish(self, topic: str, payload: bytes | str, retain: bool = False, source: ClientConn | None = None):
        """Consegna a chi è iscritto (e salva l'ultimo valore se retain). Usato anche dal server stesso."""
        p = payload.encode() if isinstance(payload, str) else payload
        if retain:
            if p:
                self.retained[topic] = p
            else:
                self.retained.pop(topic, None)  # messaggio vuoto con retain = cancella
        for c in list(self.clients.values()):
            if c.connected and c is not source and any(mp.topic_matches(f, topic) for f in c.subs):
                await c.send(topic, p)
        for fn in self.listeners:
            try:
                await fn(source, topic, p, retain)
            except Exception:
                log.exception("errore nell'elaborare un messaggio MQTT")

    async def send_direct(self, client_id: str, topic: str, payload: bytes | str) -> bool:
        """Messaggio a un client preciso, anche se non è iscritto (così funzionano i dispositivi xiaozhi)."""
        c = self.clients.get(client_id)
        if not c or not c.connected:
            return False
        await c.send(topic, payload)
        return True

    # ------------------------------------------------------------------ connessione
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        c = ClientConn(self, reader, writer, peer)
        try:
            await self._session(c)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.TimeoutError):
            pass
        except mp.MqttError as e:
            log.info("MQTT %s: pacchetto non valido (%s)", peer, e)
        except Exception:
            log.exception("MQTT %s: errore", peer)
        finally:
            await c.close()
            if c.client_id and self.clients.get(c.client_id) is c:
                del self.clients[c.client_id]
                for fn in self.disconnect_listeners:
                    try:
                        await fn(c)
                    except Exception:
                        log.exception("errore alla disconnessione MQTT")

    async def _session(self, c: ClientConn):
        ptype, flags, body = await asyncio.wait_for(mp.read_packet(c.reader), timeout=10)
        if ptype != mp.CONNECT:
            return
        info = mp.parse_connect(body)
        if len(self.clients) >= self.max_clients or \
                sum(1 for x in self.clients.values() if x.ip == c.ip) >= self.max_per_ip:
            await c.write(mp.connack(3))  # server non disponibile
            return
        auth = await self.authenticate(info["client_id"], info["username"], info["password"], c.ip)
        if auth is None:
            log.warning("MQTT: accesso rifiutato a %r (utente %r) da %s", info["client_id"], info["username"], c.ip)
            await c.write(mp.connack(5))  # non autorizzato
            return
        c.client_id = info["client_id"] or f"anonimo-{id(c)}"
        c.username, c.info = info["username"], auth
        old = self.clients.get(c.client_id)
        if old:
            await old.close()  # stesso client_id: vince la connessione nuova (come prevede MQTT)
        self.clients[c.client_id] = c
        c.connected = True
        await c.write(mp.connack(0))
        log.info("MQTT: collegato %s (%s) da %s", c.client_id, auth.get("kind"), c.ip)
        keepalive = info["keepalive"] or 0
        timeout = keepalive * 1.5 if keepalive else None
        while not c.closed:
            ptype, flags, body = await asyncio.wait_for(mp.read_packet(c.reader), timeout=timeout)
            if ptype == mp.PUBLISH:
                msg = mp.parse_publish(flags, body)
                if msg["qos"]:
                    await c.write(mp.puback(msg["packet_id"]))
                if not c.rate_ok(self.msgs_per_second):
                    continue  # troppi messaggi: scartati
                if not c.may("pub", msg["topic"]) or not mp.valid_filter(msg["topic"]) or \
                        any(ch in msg["topic"] for ch in "+#"):
                    log.info("MQTT: %s non può scrivere su %s", c.client_id, msg["topic"])
                    continue
                await self.publish(msg["topic"], msg["payload"], msg["retain"], source=c)
            elif ptype == mp.SUBSCRIBE:
                pid, topics = mp.parse_subscribe(body)
                codes = []
                for flt, qos in topics:
                    # il filtro richiesto deve stare dentro quelli permessi: i caratteri jolly richiesti
                    # vengono trattati come un livello qualsiasi ("#" da solo non sta dentro "scuola/#")
                    if mp.valid_filter(flt) and c.may("sub", flt.replace("#", "x").replace("+", "x")):
                        c.subs[flt] = qos
                        codes.append(0)  # concesso con QoS 0
                    else:
                        codes.append(0x80)
                await c.write(mp.suback(pid, codes))
                for flt, code in zip([t for t, _ in topics], codes):
                    if code == 0:
                        for topic, payload in list(self.retained.items()):
                            if mp.topic_matches(flt, topic):
                                await c.send(topic, payload, retain=True)
            elif ptype == mp.UNSUBSCRIBE:
                pid, topics = mp.parse_unsubscribe(body)
                for t in topics:
                    c.subs.pop(t, None)
                await c.write(mp.unsuback(pid))
            elif ptype == mp.PINGREQ:
                await c.write(mp.PINGRESP_PKT)
            elif ptype == mp.DISCONNECT:
                return
            # PUBACK & co. ricevuti: niente da fare (consegniamo con QoS 0)

    def stats(self) -> dict:
        kinds = defaultdict(int)
        for c in self.clients.values():
            kinds[c.info.get("kind", "?")] += 1
        return {"port": self.port, "clients": len(self.clients), "by_kind": dict(kinds), "retained": len(self.retained)}
