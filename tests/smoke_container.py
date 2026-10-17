"""Prova veloce di un'installazione appena avviata (container Docker o Kubernetes), usata dalla verifica su GitHub.

Primo accesso con admin / Passw0rd!, cambio obbligatorio della password, una domanda scritta nella pagina
dell'assistente con risposta a voce, pagine principali e metriche spente di serie.
Uso:  python tests/smoke_container.py https://127.0.0.1:8443 [PASSWORD]
      con PASSWORD (installazione già configurata, es. dopo un riavvio) si entra direttamente con quella.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import aiohttp

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://127.0.0.1:8443"
KNOWN = sys.argv[2] if len(sys.argv) > 2 else ""
H = {"X-Requested-With": "husky"}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


async def wait_up(http, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            async with http.get(BASE + "/health") as r:
                if r.status == 200:
                    return await r.json()
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(5)
    return None


async def main():
    conn = aiohttp.TCPConnector(ssl=False)  # certificato autofirmato del server
    async with aiohttp.ClientSession(connector=conn, cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
        health = await wait_up(http)
        check("Server acceso (health)", health and health.get("ok"), str(health))
        if not health:
            return 1
        new = KNOWN or "Container-Prova-2026!"
        if KNOWN:  # dopo un riavvio: utenti e password devono essere ancora lì (volume dei dati)
            async with http.post(BASE + "/api/login", json={"username": "admin", "password": "Passw0rd!"}, headers=H) as r:
                check("Dopo il riavvio la password predefinita non vale più", r.status == 401)
        else:
            async with http.post(BASE + "/api/login", json={"username": "admin", "password": "Passw0rd!"}, headers=H) as r:
                check("Primo accesso con admin / Passw0rd!", r.status == 200)
            async with http.get(BASE + "/api/admin/status") as r:
                check("Prima del cambio della password non si fa altro", r.status == 403)
            async with http.post(BASE + "/api/account/password", json={"old": "Passw0rd!", "new": new}, headers=H) as r:
                check("Password cambiata", r.status == 200)
        async with http.post(BASE + "/api/login", json={"username": "admin", "password": new}, headers=H) as r:
            check("Accesso con la password nuova", r.status == 200)
        async with http.get(BASE + "/api/admin/status") as r:
            st = await r.json()
        check("Pagina Admin: stato, versione e modello", r.status == 200 and st.get("version"), f"{st.get('version')} {st.get('model')}")
        for page in ("/", "/admin", "/cronologia", "/account", "/lezioni"):
            async with http.get(BASE + page) as r:
                check(f"Pagina {page}", r.status == 200)
        async with http.get(BASE + "/metrics") as r:
            check("Metriche spente di serie", r.status == 404)
        msgs, audio = [], 0
        async with http.ws_connect(BASE.replace("https", "wss") + "/xiaozhi/v1/") as ws:
            await ws.send_str(json.dumps({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}}))
            await ws.send_str(json.dumps({"type": "text", "text": "Ciao, come ti chiami?"}))
            deadline = time.time() + 300
            while time.time() < deadline:
                msg = await ws.receive(timeout=300)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    audio += len(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    msgs.append(m)
                    if m.get("type") == "tts" and m.get("state") == "stop":
                        break
                else:
                    break
        said = " ".join(m.get("text", "") for m in msgs if m.get("type") == "tts" and m.get("state") == "sentence_start")
        check("Domanda scritta: risposta del modello letta ad alta voce", said and audio > 10000, f"{said[:60]} ({audio} byte)")
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} controlli superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
