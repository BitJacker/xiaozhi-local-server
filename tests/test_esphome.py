"""Test end-to-end di ESPHome con l'API nativa: due schede finte che parlano il protocollo vero (aioesphomeapi lato
server, tests/fake_esphome.py lato dispositivo).

Approvazione dalla pagina Admin, sensori e comandi come strumenti, scheda vocale con altoparlante (voce in streaming)
e con media player (file WAV), frase non capita, annunci (Avvisi), ricerca in rete (mDNS), chiave sbagliata, revoca.
Serve Ollama acceso (oppure --mock).  Uso:  python tests/test_esphome.py [--mock]
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from fake_esphome import ANNOUNCE, API_AUDIO, SPEAKER, VA, FakeEsphome  # noqa: E402
from run_all import MOCK, PY, env, make_config, stop, wait_health  # noqa: E402
from test_suite import speech  # noqa: E402

from xiaozhi_locale.auth import Auth  # noqa: E402

PORT = 8098
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "tests" / "_data_esphome"
H = {"X-Requested-With": "husky"}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


async def until(pred, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = pred()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return r
        await asyncio.sleep(0.2)
    return None


async def ask_text(s: aiohttp.ClientSession, text: str) -> str:
    """Una domanda scritta dalla pagina dell'assistente (WebSocket del client web)."""
    async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/") as ws:
        await ws.send_str(json.dumps({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}}))
        await ws.send_str(json.dumps({"type": "text", "text": text}))
        said = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 120)
            if msg.type != aiohttp.WSMsgType.TEXT and msg.type != aiohttp.WSMsgType.BINARY:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                o = json.loads(msg.data)
                if o.get("type") == "tts" and o.get("state") == "sentence_start":
                    said.append(o.get("text", ""))
                if o.get("type") == "tts" and o.get("state") == "stop":
                    break
        return " ".join(said)


async def main_async(log_path: Path) -> None:
    box = FakeEsphome("aula1", VA | SPEAKER | API_AUDIO | ANNOUNCE)   # scheda con altoparlante (voce in streaming)
    media = FakeEsphome("aula2", VA | API_AUDIO)                      # scheda con media player (scarica un WAV)
    await box.start()
    await media.start()
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    try:
        async with s.post(BASE + "/api/login", json={"username": "prof", "password": "Password-Prof-123"}, headers=H) as r:
            assert r.status == 200, await r.text()

        async def esp(data=None):
            if data is None:
                async with s.get(BASE + "/api/admin/esphome") as r:
                    return r.status, await r.json()
            async with s.post(BASE + "/api/admin/esphome", json=data, headers=H) as r:
                return r.status, await r.json()

        # ---------------- ricerca in rete (mDNS): il finto dispositivo si annuncia come uno vero
        st, res = await esp({"action": "discovery", "enabled": True})
        zc = info = None
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
            zc = AsyncZeroconf(interfaces=["127.0.0.1"])
            info = ServiceInfo("_esphomelib._tcp.local.", "laboratorio._esphomelib._tcp.local.",
                               addresses=[socket.inet_aton("127.0.0.1")], port=box.port,
                               properties={"mac": "aabbcc000001", "version": "2026.9.0", "board": "esp32-s3-box"},
                               server="laboratorio.local.")
            await zc.async_register_service(info)
        except Exception as e:
            print("   (annuncio mDNS non possibile qui:", e, ")")
        found = await until(lambda: esp_found(s), 15)
        check("Ricerca in rete accesa: il dispositivo ESPHome compare tra quelli trovati (mDNS)",
              st == 200 and res.get("discovery") and found, str(found)[:120])

        # ---------------- approvazione: chiave sbagliata rifiutata, poi le due schede
        st, res = await esp({"action": "save", "name": "aula1", "host": "127.0.0.1", "port": box.port, "key": "corta"})
        check("Chiave di cifratura scritta male: rifiutata", st == 400 and "Chiave" in res.get("error", ""), res.get("error", ""))
        st, res = await esp({"action": "save", "name": "aula1", "label": "Box aula 1", "host": "127.0.0.1",
                             "port": box.port, "voice": True})
        st2, _ = await esp({"action": "save", "name": "aula2", "host": "127.0.0.1", "port": media.port, "voice": True})
        ok = await until(lambda: box.va_subscribed.is_set() and media.va_subscribed.is_set(), 20)
        _, res = await esp()
        dev = next((d for d in res["devices"] if d["name"] == "aula1"), {})
        check("Schede approvate: collegate con l'API nativa, entità lette, assistente vocale attivo",
              st == 200 and st2 == 200 and ok and dev.get("connected") and dev.get("voice_capable")
              and any(e["name"] == "Temperatura" and "21.5" in str(e["value"]) for e in dev.get("entities", [])),
              json.dumps(dev)[:200])
        check("Pagina Admin: dispositivo approvato non più tra quelli 'trovati'",
              not any(f["port"] == box.port for f in res.get("found", [])))

        # ---------------- sensori e comandi come strumenti (domande scritte dall'admin)
        a = await ask_text(s, "Usa lo strumento esphome_aula1 e dimmi la temperatura.")
        check("Strumento ESPHome: lettura del sensore", "21" in a, a[:100])
        await ask_text(s, "Usa lo strumento aula1_ventola con stato=on")
        check("Strumento ESPHome: comando all'interruttore arrivato al dispositivo", await until(lambda: box.switch, 10))
        await ask_text(s, "Usa lo strumento aula1_campanella")
        check("Strumento ESPHome: pulsante premuto", await until(lambda: box.pressed == 1, 10))

        # ---------------- scheda vocale con altoparlante: voce in streaming
        pcm = speech("Quante zampe ha un gatto?", 1.5).tobytes()
        resp = await box.ask(pcm)
        end = await box.wait_event("RUN_END", 120)
        names = [n for n, _ in box.events]
        stt = dict(box.events).get("STT_END", {}).get("text", "")
        check("Scheda vocale: il server accetta la voce via API (porta 0, nessun errore)",
              not resp.error and resp.port == 0, f"port={resp.port}")
        check("Scheda vocale: eventi nell'ordine giusto (inizio, fine parlato, testo, risposta, fine)",
              end is not None and names.index("RUN_START") < names.index("STT_VAD_END") < names.index("STT_END")
              < names.index("TTS_STREAM_START") < names.index("TTS_STREAM_END") < names.index("RUN_END"), str(names))
        check("Scheda vocale: la domanda è stata capita", any(w in stt.lower() for w in ("gatto", "zamp")), stt)
        secs = len(box.audio_in) / 2 / 16000
        check("Scheda vocale: voce della risposta in streaming (PCM 16 kHz)", secs > 1, f"{secs:.1f} s")

        # ---------------- scheda con media player: file WAV da scaricare
        await media.ask(pcm)
        await media.wait_event("RUN_END", 120)
        tts_end = dict(media.events).get("TTS_END", {})
        wav_ok = False
        if tts_end.get("url"):
            async with aiohttp.ClientSession() as anon:
                async with anon.get(tts_end["url"]) as r:
                    body = await r.read()
                    wav_ok = r.status == 200 and body[:4] == b"RIFF" and len(body) > 20000
        check("Scheda con media player: risposta come file WAV scaricabile",
              "TTS_START" in dict(media.events) and wav_ok and not media.audio_in, tts_end.get("url", "")[-40:])
        async with aiohttp.ClientSession() as anon:
            async with anon.get(BASE + "/xiaozhi/esphome/inventato.wav") as r:
                check("Indirizzi WAV inventati: nessun audio", r.status == 404)

        # ---------------- solo silenzio: errore "non ho capito" e fine
        await box.ask(bytes(16000 * 2 * 7))  # 7 secondi di silenzio (limite: esphome.no_speech_seconds = 5)
        err = await box.wait_event("ERROR", 30)
        check("Silenzio: errore stt-no-text-recognized e fine della richiesta",
              err and err.get("code") == "stt-no-text-recognized" and await box.wait_event("RUN_END", 5) is not None)

        # ---------------- avvisi: annuncio sulla scheda che lo permette
        async with s.post(BASE + "/api/admin/alerts", json={"action": "send", "text": "Ricreazione tra cinque minuti.",
                                                            "target": "all", "speak": True}, headers=H) as r:
            sent = (await r.json()).get("sent", {})
        url = box.announces[0].media_id if box.announces else ""
        check("Avviso: annuncio vocale sulla scheda ESPHome (testo e WAV)",
              sent.get("esphome") == 1 and box.announces and box.announces[0].text.startswith("Ricreazione")
              and url.endswith(".wav"), str(sent))
        async with s.get(BASE + "/api/admin/alerts") as r:
            online = (await r.json()).get("online", [])
        check("Avvisi: le schede ESPHome risultano collegate", "esphome:aula1" in online, str(online))

        # ---------------- voce tolta dall'admin: la scheda non può più fare domande
        await esp({"action": "save", "id": dev["id"], "name": "aula1", "host": "127.0.0.1", "port": box.port, "voice": False})
        await until(lambda: box.connected.is_set(), 15)
        box.va_subscribed.clear()
        await asyncio.sleep(2)
        check("Voce disattivata: la scheda non si iscrive più all'assistente", not box.va_subscribed.is_set())

        # ---------------- eliminato: scollegato subito
        _, res = await esp()
        d2 = next(d for d in res["devices"] if d["name"] == "aula2")
        await esp({"action": "delete", "id": d2["id"]})
        check("Dispositivo eliminato: scollegato subito", await until(lambda: media.disconnected.is_set(), 10))

        # ---------------- chiave di cifratura su un dispositivo che non la usa: errore chiaro, nessun dato
        await esp({"action": "save", "id": dev["id"], "name": "aula1", "host": "127.0.0.1", "port": box.port,
                   "key": "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleTEyMzQ=", "voice": False})
        bad = await until(lambda: esp_dev(s, "aula1", lambda x: x["error"] and not x["connected"]), 20)
        check("Chiave di cifratura sbagliata: non collegato, errore visibile nella pagina Admin", bad, str(bad)[:120])
        if zc:
            await zc.async_unregister_service(info)
            await zc.async_close()
        await firmware_checks(s)
    finally:
        await s.close()
        await box.stop()
        await media.stop()


async def firmware_checks(s):
    """Firmware da installare dal browser: caricamento, controlli, manifest per ESP Web Tools, solo admin."""
    import hashlib
    import os

    def form(data: bytes, chip="ESP32-S3"):
        f = aiohttp.FormData()
        f.add_field("name", "Husky box")
        f.add_field("version", "2.0.1")
        f.add_field("chip", chip)
        f.add_field("file", data, filename="merged.bin", content_type="application/octet-stream")
        return f
    good = b"\xe9" + os.urandom(300_000)
    async with s.post(BASE + "/api/admin/firmware-upload", data=form(os.urandom(300_000)), headers=H) as r:
        bad = r.status, (await r.json()).get("error", "")
    check("Firmware: un file che non è un firmware completo viene rifiutato", bad[0] == 400, bad[1][:60])
    async with s.post(BASE + "/api/admin/firmware-upload", data=form(good, "ESP99"), headers=H) as r:
        check("Firmware: chip sconosciuto rifiutato", r.status == 400)
    async with s.post(BASE + "/api/admin/firmware-upload", data=form(good), headers=H) as r:
        item = (await r.json()).get("item") or {}
    async with s.get(BASE + f"/admin/firmware/{item.get('id')}/manifest.json") as r:
        man = await r.json(content_type=None) if r.status == 200 else {}
    async with s.get(BASE + f"/admin/firmware/{item.get('id')}/firmware.bin") as r:
        same = hashlib.sha256(await r.read()).hexdigest() == hashlib.sha256(good).hexdigest()
    check("Firmware caricato: manifest per ESP Web Tools (chip, indirizzo 0) e file identico",
          man.get("builds", [{}])[0].get("chipFamily") == "ESP32-S3"
          and man["builds"][0]["parts"][0]["offset"] == 0 and same, json.dumps(man)[:120])
    stud = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with stud.post(BASE + "/api/login", json={"username": "studente", "password": "Password-Stud-123"}, headers=H) as r:
        pass
    async with stud.get(BASE + f"/admin/firmware/{item.get('id')}/firmware.bin", allow_redirects=False) as r:
        st1 = r.status
    async with stud.post(BASE + "/api/admin/firmware-upload", data=form(good), headers=H) as r:
        st2 = r.status
    await stud.close()
    check("Firmware: uno studente non può scaricarlo né caricarne", st1 in (302, 403) and st2 == 403, f"{st1} {st2}")
    async with s.get(BASE + "/static/vendor/esp-web-tools/install-button.js") as r:
        check("ESP Web Tools incluso nel server (nessun file da Internet)", r.status == 200)
    async with s.request("DELETE", BASE + "/api/admin/firmware", json={"id": item.get("id")}, headers=H) as r:
        items = (await r.json()).get("items")
    async with s.get(BASE + f"/admin/firmware/{item.get('id')}/firmware.bin") as r:
        check("Firmware eliminato: file non più disponibile", items == [] and r.status == 404)


async def esp_found(s):
    async with s.get(BASE + "/api/admin/esphome") as r:
        return [f for f in (await r.json()).get("found", []) if f["node"] == "laboratorio"]


async def esp_dev(s, name, pred):
    async with s.get(BASE + "/api/admin/esphome") as r:
        d = next((x for x in (await r.json())["devices"] if x["name"] == name), None)
    return d if d and pred(d) else None


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    a = Auth({"enabled": True}, DATA)
    a.create_user("prof", "Password-Prof-123", "admin")
    a.create_user("studente", "Password-Stud-123", "user")
    cfg = make_config(PORT, "tests/_data_esphome", auth=True, turns=100, huskylens=False)
    cfg = cfg.replace("tool_groups_threshold: 12", "tool_groups_threshold: 0")
    cfg = cfg.replace("server:\n", "server:\n  public_host: 127.0.0.1\n", 1)
    (ROOT / "tests" / "config.esphome.yaml").write_text(cfg, encoding="utf-8")
    procs = []
    if MOCK and not _port_open(1299):
        procs.append(subprocess.Popen([PY, "tests/mock_llm.py"], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    log = open(DATA / "server.log", "w", encoding="utf-8")
    procs.append(subprocess.Popen([PY, "server.py", "-c", "tests/config.esphome.yaml"], cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT))
    try:
        if not wait_health(BASE + "/health", '"ok"'):
            print("server non partito:\n" + (DATA / "server.log").read_text(encoding="utf-8")[-2000:])
            return 1
        asyncio.run(main_async(DATA / "server.log"))
    finally:
        stop(procs)
        log.close()
    if all(RESULTS):
        shutil.rmtree(DATA, ignore_errors=True)
    (ROOT / "tests" / "config.esphome.yaml").unlink(missing_ok=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test ESPHome superati ====")
    return 0 if all(RESULTS) else 1


def _port_open(port: int) -> bool:
    with socket.socket() as so:
        so.settimeout(0.3)
        return so.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    sys.exit(main())
