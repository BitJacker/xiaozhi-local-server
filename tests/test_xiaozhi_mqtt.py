"""Test end-to-end del protocollo xiaozhi MQTT+UDP e degli avvisi ai dispositivi.

Un dispositivo simulato fa esattamente quello che fa il firmware xiaozhi-esp32:
OTA -> riceve i dati MQTT -> si collega al broker (paho-mqtt) con client_id GID@@@mac@@@uuid e il suo token ->
"hello" con transport udp -> riceve chiave e nonce AES -> manda la voce in pacchetti UDP cifrati AES-128-CTR ->
riceve trascrizione e risposta via MQTT e la voce via UDP cifrata. Poi: notifica vocale dalla pagina Admin
(scarica l'audio Ogg Opus), dispositivo non approvato rifiutato, revoca.
Serve Ollama acceso (oppure --mock).  Uso:  python tests/test_xiaozhi_mqtt.py [--mock]
"""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import aiohttp
import paho.mqtt.client as paho

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from run_all import PY, env, make_config, stop, wait_health  # noqa: E402
from test_suite import speech  # noqa: E402

from xiaozhi_locale.audio import OpusDecoder, OpusEncoder  # noqa: E402
from xiaozhi_locale.auth import Auth  # noqa: E402
from xiaozhi_locale.xiaozhi_mqtt import HEADER, aes_ctr  # noqa: E402

PORT, MQTT_PORT, UDP_PORT = 8096, 18830, 18840
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "tests" / "_data_xzmqtt"
MAC, UUID = "aa:bb:cc:12:34:56", "uuid-mqtt-1"
H = {"X-Requested-With": "husky"}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


def wait(pred, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Device:
    """Dispositivo xiaozhi simulato: MQTT per i comandi, UDP cifrato per la voce."""

    def __init__(self, mqtt_cfg: dict):
        self.cfg = mqtt_cfg
        self.msgs: list[dict] = []
        self.c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=mqtt_cfg["client_id"])
        self.c.username_pw_set(mqtt_cfg["username"], mqtt_cfg["password"])
        self.rc = []
        self.c.on_connect = lambda cl, ud, f, reason, p: self.rc.append(reason.value)
        self.c.on_message = lambda cl, ud, m: self.msgs.append({"_topic": m.topic, **json.loads(m.payload)})
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.settimeout(0.2)
        self.audio_in: list[bytes] = []
        self.seq = 0

    def connect(self):
        host, port = self.cfg["endpoint"].split(":")
        self.c.connect(host, int(port), keepalive=60)
        self.c.loop_start()
        wait(lambda: self.rc, 5)
        return self.rc[0] if self.rc else None

    def send(self, obj):  # il firmware pubblica su publish_topic, senza iscriversi a niente
        self.c.publish(self.cfg["publish_topic"], json.dumps(obj))

    def find(self, pred, timeout=60.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for m in self.msgs:
                if pred(m):
                    return m
            time.sleep(0.05)
        return None

    def open_udp(self, hello):
        u = hello["udp"]
        self.key, self.nonce = bytes.fromhex(u["key"]), bytes.fromhex(u["nonce"])
        self.addr = (u["server"], u["port"])
        self.ssrc = struct.unpack_from(">I", self.nonce, 4)[0]
        threading.Thread(target=self._recv, daemon=True).start()

    def send_audio(self, packets, tamper=False):
        for i, p in enumerate(packets):
            self.seq += 1
            header = HEADER.pack(1, 0, len(p), self.ssrc, i * 60, self.seq)
            data = header + aes_ctr(self.key, header, p)
            if tamper:
                data = header + bytes(len(p))  # chiave sbagliata: il server non deve capire niente
            self.udp.sendto(data, self.addr)
            time.sleep(0.06)

    def _recv(self):
        while True:
            try:
                data, _ = self.udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            _t, _f, length, ssrc, _ts, _seq = HEADER.unpack_from(data)
            if ssrc == self.ssrc and len(data) == 16 + length:
                self.audio_in.append(aes_ctr(self.key, data[:16], data[16:]))

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()
        self.udp.close()


async def admin_session():
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with s.post(BASE + "/api/login", json={"username": "prof", "password": "Password-Prof-123"}, headers=H) as r:
        assert r.status == 200, await r.text()
    return s


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    # ---------------- preparazione: admin, dispositivo approvato, MQTT acceso (come dalla pagina Admin)
    a = Auth({"enabled": True}, DATA)
    a.create_user("prof", "Password-Prof-123", "admin")
    code = a.device_status(MAC, UUID)["code"]
    a.approve_device(code=code, name="Robot MQTT")
    (DATA / "mqtt.json").write_text(json.dumps({"enabled": True, "xiaozhi_mqtt": True, "accounts": [], "external": []}),
                                    encoding="utf-8")
    cfg = make_config(PORT, "tests/_data_xzmqtt", auth=True, turns=50, huskylens=False)
    cfg = cfg.replace("  port: 1883 ", f"  port: {MQTT_PORT} ").replace("  udp_port: 8884 ", f"  udp_port: {UDP_PORT} ")
    cfg = cfg.replace("server:\n", "server:\n  public_host: 127.0.0.1\n", 1)
    (ROOT / "tests" / "config.xzmqtt.yaml").write_text(cfg, encoding="utf-8")
    log = open(DATA / "server.log", "w", encoding="utf-8")
    srv = subprocess.Popen([PY, "server.py", "-c", "tests/config.xzmqtt.yaml"], cwd=ROOT, env=env, stdout=log,
                           stderr=subprocess.STDOUT)
    dev = None
    try:
        if not wait_health(BASE + "/health", '"ok"'):
            print("server non partito:\n" + (DATA / "server.log").read_text(encoding="utf-8")[-2000:])
            return 1
        # ---------------- OTA: il dispositivo approvato riceve anche la configurazione MQTT
        req = urllib.request.Request(BASE + "/xiaozhi/ota/", data=b'{"application":{"version":"1.9.0"}}',
                                     headers={"Device-Id": MAC, "Client-Id": UUID, "Content-Type": "application/json"})
        ota = json.loads(urllib.request.urlopen(req, timeout=10).read())
        m = ota.get("mqtt") or {}
        check("OTA: il dispositivo approvato riceve i dati MQTT (endpoint, client_id, token, argomento)",
              m.get("endpoint", "").endswith(f":{MQTT_PORT}") and "@@@" in m.get("client_id", "")
              and m.get("publish_topic") == "device-server" and m.get("password"), json.dumps(m)[:150])
        req2 = urllib.request.Request(BASE + "/xiaozhi/ota/", data=b"{}", headers={"Device-Id": "aa:aa:aa:aa:aa:aa",
                                                                                  "Client-Id": "ladro", "Content-Type": "application/json"})
        ota2 = json.loads(urllib.request.urlopen(req2, timeout=10).read())
        check("OTA: un dispositivo non approvato NON riceve i dati MQTT", "mqtt" not in ota2 and "activation" in ota2)

        thief = Device({**m, "client_id": m["client_id"].replace(UUID, "ladro")})
        check("Broker: stesso MAC con un altro Client-Id rifiutato", thief.connect() not in (0, None))
        thief.close()
        dev = Device(m)
        check("Broker: il dispositivo si collega con il suo token (paho-mqtt)", dev.connect() == 0)

        # ---------------- hello -> canale UDP
        dev.send({"type": "hello", "version": 3, "transport": "udp", "features": {"mcp": True},
                  "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
        hello = dev.find(lambda x: x.get("type") == "hello", 10)
        u = (hello or {}).get("udp") or {}
        check("Hello: risposta con server UDP, chiave e nonce AES-128 (16 byte)",
              hello and hello.get("transport") == "udp" and len(bytes.fromhex(u.get("key", ""))) == 16
              and len(bytes.fromhex(u.get("nonce", ""))) == 16 and u.get("port") == UDP_PORT, json.dumps(hello)[:160])
        check("Hello: risposta arrivata sull'argomento del dispositivo senza iscriversi",
              hello and hello["_topic"] == "devices/p2p/" + MAC.replace(":", "_"))
        dev.open_udp(hello)
        sid = hello["session_id"]

        # ---------------- una domanda a voce via UDP cifrato
        dev.send({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"})
        packets = OpusEncoder(16000, 60).encode(speech("Quante zampe ha un gatto?", 0.3))
        dev.send_audio(packets)
        dev.send({"session_id": sid, "type": "listen", "state": "stop"})
        stt = dev.find(lambda x: x.get("type") == "stt", 60)
        check("Voce cifrata via UDP capita dal server (trascrizione via MQTT)",
              stt and "zamp" in stt.get("text", "").lower(), (stt or {}).get("text", ""))
        stop_msg = dev.find(lambda x: x.get("type") == "tts" and x.get("state") == "stop", 90)
        answer = " ".join(x.get("text", "") for x in dev.msgs if x.get("type") == "tts" and x.get("state") == "sentence_start")
        check("Risposta del modello arrivata via MQTT", stop_msg is not None and answer, answer[:80])
        dec = OpusDecoder(24000)
        samples = sum(len(dec.decode(p)) for p in dev.audio_in)
        check("Voce della risposta ricevuta via UDP e decifrata correttamente (Opus valido)",
              samples > 16000, f"{len(dev.audio_in)} pacchetti, {samples / 16000:.1f} s")

        # ---------------- pacchetti falsi o ripetuti: ignorati
        n_stt = sum(1 for x in dev.msgs if x.get("type") == "stt")
        dev.send({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"})
        dev.send_audio(packets[:20], tamper=True)
        dev.send({"session_id": sid, "type": "listen", "state": "stop"})
        time.sleep(4)
        new_stt = [x.get("text") for x in dev.msgs if x.get("type") == "stt"][n_stt:]
        check("Audio cifrato con una chiave sbagliata: nessuna domanda capita", not any(new_stt), str(new_stt))

        # ---------------- avviso vocale dalla pagina Admin (notify) e avvisi programmati
        async def admin_part():
            s = await admin_session()
            async with s.get(BASE + "/api/admin/alerts") as r:
                online = (await r.json()).get("online", [])
            check("Avvisi: il dispositivo MQTT risulta collegato", MAC in online, str(online))
            async with s.post(BASE + "/api/admin/alerts", json={"action": "send", "text": "Fine dell'attività tra cinque minuti.",
                                                                "target": "all", "speak": True}, headers=H) as r:
                res = await r.json()
            note = dev.find(lambda x: x.get("type") == "notify", 30)
            ok_audio = False
            if note:
                async with aiohttp.ClientSession() as anon:
                    async with anon.get(note["audio_url"]) as r:
                        body = await r.read()
                        ok_audio = r.status == 200 and body[:4] == b"OggS" and r.content_type == "audio/ogg"
            check("Avviso vocale: il dispositivo riceve notify con sottotitolo e l'audio Ogg Opus si scarica",
                  res.get("ok") and note and note["subtitles"][0]["text"].startswith("Fine") and ok_audio,
                  json.dumps(note)[:140] if note else str(res))
            async with aiohttp.ClientSession() as anon:
                async with anon.get(BASE + f"/xiaozhi/notify/{secrets.token_urlsafe(16)}.ogg") as r:
                    check("Avviso vocale: indirizzi inventati non danno nessun audio", r.status == 404)
            async with s.post(BASE + "/api/admin/alerts", json={"action": "save", "text": "Ricreazione!", "time": "10:55",
                                                                "days": [0, 1, 2, 3, 4]}, headers=H) as r:
                sch = (await r.json()).get("scheduled", [])
            async with s.post(BASE + "/api/admin/alerts", json={"action": "save", "text": "x", "time": "25:99"}, headers=H) as r:
                bad_time = r.status
            check("Avvisi programmati: salvati, orario sbagliato rifiutato",
                  len(sch) == 1 and sch[0]["time"] == "10:55" and bad_time == 400)
            async with s.get(BASE + "/api/admin/status") as r:
                st = (await r.json()).get("mqtt", {})
            check("Pagina Admin: stato MQTT con il dispositivo xiaozhi collegato",
                  st.get("running") and st.get("broker", {}).get("by_kind", {}).get("xiaozhi") == 1, str(st)[:160])
            # ---------------- revoca: sessione e connessione MQTT chiuse subito
            async with s.request("DELETE", BASE + "/api/admin/devices", json={"id": f"{MAC}|{UUID}"}, headers=H) as r:
                pass
            check("Dispositivo revocato: la connessione MQTT viene chiusa subito", wait(lambda: not dev.c.is_connected(), 5))
            again = Device(m)
            check("Dispositivo revocato: il vecchio token non entra più nel broker", again.connect() not in (0, None))
            again.close()
            await s.close()
        asyncio.run(admin_part())
    finally:
        if dev:
            dev.close()
        stop([srv])
        log.close()
    shutil.rmtree(DATA, ignore_errors=True)
    (ROOT / "tests" / "config.xzmqtt.yaml").unlink(missing_ok=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test xiaozhi MQTT+UDP superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
