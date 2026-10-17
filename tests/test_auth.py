"""Test di sicurezza: account, 2FA, protezioni anti-bot, dispositivi, permessi.

Lo lancia tests/run_all.py con un server dedicato (account attivi, cartella dati di test).
Uso:  python tests/test_auth.py http://localhost:8081 percorso/del/log/server
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale.auth import totp  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8081"
LOG = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "tests" / "_data_auth" / "server.log"
H = {"X-Requested-With": "husky"}
RESULTS: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def post(s, path, data, headers=H):
    async with s.post(BASE + path, json=data, headers=headers) as r:
        return r.status, await r.json(content_type=None)


async def get(s, path):
    async with s.get(BASE + path, allow_redirects=False) as r:
        try:
            return r.status, await r.json(content_type=None), r.headers
        except Exception:
            return r.status, None, r.headers


async def login(username, password, code=""):
    s = new_session()
    st, d = await post(s, "/api/login", {"username": username, "password": password, "code": code})
    return s, st, d


async def ask(s_cookies, text, extra_headers=None):
    """Apre il WebSocket (con i cookie della sessione) e fa una domanda scritta."""
    async with s_cookies.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/?device-id=web",
                                    headers=extra_headers or {}) as ws:
        await ws.send_json({"type": "hello", "transport": "websocket",
                            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1}})
        await ws.receive(timeout=10)
        await ws.send_json({"type": "text", "text": text})
        parts = []
        while True:
            m = await ws.receive(timeout=90)
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            if d["type"] == "tts" and d.get("state") == "sentence_start":
                parts.append(d["text"])
            if d["type"] == "tts" and d.get("state") == "stop":
                return " ".join(parts)


async def main():
    anon = new_session()

    # ------------------------------------------------ primo avvio
    st, d, _ = await get(anon, "/api/auth/state")
    check("Primo avvio: serve creare l'admin", d["needs_setup"] and d["auth_enabled"])
    st, _, hd = await get(anon, "/")
    check("Pagine protette: / rimanda al login", st == 302 and hd["Location"].startswith("/login"), f"HTTP {st}")
    st, _, _ = await get(anon, "/api/history")
    check("API protette senza login: 401", st == 401)
    codes = {}
    for path in ("/api/admin/settings", "/api/admin/models", "/api/admin/rooms", "/api/admin/music", "/api/admin/backup",
                 "/api/admin/backup-file?name=husky-20260101-000000.husky-backup", "/api/voiceprint", "/api/memory"):
        codes[path] = (await get(anon, path))[0]
    for path in ("/api/admin/music-upload", "/api/voiceprint", "/api/admin/backup"):
        codes["POST " + path] = (await post(anon, path, {"action": "run"}))[0]
    check("Nuove API (impostazioni, modelli, aule, musica, backup, impronta) senza login: 401",
          all(c == 401 for c in codes.values()), str({k: v for k, v in codes.items() if v != 401}))
    st, _, _ = await get(anon, "/metrics")
    check("Metriche spente di serie (404 anche senza login)", st == 404)
    st, d = await post(anon, "/api/setup", {"code": "00000000", "username": "admin", "password": "Password-Admin-1"})
    check("Setup con codice sbagliato rifiutato", st == 403)
    m = None
    for _ in range(30):  # il codice compare nel terminale (log) del server
        m = re.search(r"questo codice:\s+(\d{8})", LOG.read_text(encoding="utf-8", errors="replace"))
        if m:
            break
        await asyncio.sleep(1)
    code = m.group(1)
    st, d = await post(anon, "/api/setup", {"code": code, "username": "admin", "password": "corta"})
    check("Password troppo corta rifiutata", st == 400, d.get("error", ""))
    st, d = await post(anon, "/api/setup", {"code": code, "username": "admin", "password": "Password-Admin-1"})
    check("Setup admin con il codice del terminale", st == 200 and d.get("ok"))
    st, d = await post(new_session(), "/api/setup", {"code": code, "username": "altro", "password": "Password-Altro-1"})
    check("Setup non ripetibile dopo il primo admin", st == 403)
    admin = anon  # ha il cookie di sessione

    # ------------------------------------------------ CSRF e intestazioni
    st, d = await post(admin, "/api/admin/users", {"action": "create", "username": "x", "password": "y"}, headers={})
    check("CSRF: POST senza intestazione rifiutato", st == 403)
    async with admin.get(BASE + "/admin") as r:
        hdr = r.headers
    check("Intestazioni di sicurezza sulle pagine",
          hdr.get("X-Frame-Options") == "DENY" and "frame-ancestors 'none'" in hdr.get("Content-Security-Policy", ""))
    s2 = new_session()
    async with s2.post(BASE + "/api/login", json={"username": "admin", "password": "Password-Admin-1"}, headers=H) as r:
        sc = r.headers.get("Set-Cookie", "")
    check("Cookie di sessione HttpOnly e SameSite=Strict", "HttpOnly" in sc and "SameSite=Strict" in sc)

    # ------------------------------------------------ utenti e permessi
    st, d = await post(admin, "/api/admin/users", {"action": "create", "username": "utente1", "password": "Password-Stud-1"})
    check("Admin crea un utente", st == 200 and any(u["username"] == "utente1" for u in d["users"]))
    st, d = await post(admin, "/api/admin/users", {"action": "create", "username": "utente1", "password": "Password-Stud-1"})
    check("Username duplicato rifiutato", st == 400)
    stud, st, _ = await login("utente1", "Password-Stud-1")
    check("Login utente", st == 200)
    st, _, _ = await get(stud, "/api/admin/users")
    check("Utente normale: API admin vietate (403)", st == 403)
    st, _, hd = await get(stud, "/admin")
    check("Utente normale: pagina admin non accessibile", st == 302 and hd["Location"] == "/")
    st, d = await post(admin, "/api/admin/users", {"action": "role", "username": "admin", "role": "user"})
    check("Non si può togliere l'ultimo admin", st == 400)

    # ------------------------------------------------ forza bruta
    await post(admin, "/api/admin/users", {"action": "create", "username": "bersaglio", "password": "Password-Giusta-1"})
    for i in range(5):
        await login("bersaglio", f"sbagliata{i}")
    _, st, d = await login("bersaglio", "Password-Giusta-1")
    check("Dopo 5 password sbagliate l'account è bloccato (anche con quella giusta)", st == 429, d.get("error", ""))
    _, st, _ = await login("utente1", "Password-Stud-1")
    check("Il blocco riguarda solo quell'account", st == 200)

    # ------------------------------------------------ 2FA
    st, d = await post(stud, "/api/account/2fa-begin", {})
    secret = d["secret"]
    check("2FA: QR code e segreto generati", "<svg" in d.get("qr", "") and d["uri"].startswith("otpauth://totp/"))
    st, d = await post(stud, "/api/account/2fa-confirm", {"code": "000000" if totp(secret, int(time.time() // 30)) != "000000" else "111111"})
    check("2FA: codice sbagliato non attiva", st == 400)
    good = totp(secret, int(time.time() // 30))
    st, d = await post(stud, "/api/account/2fa-confirm", {"code": good})
    check("2FA: attivata con il codice giusto", st == 200)
    _, st, d = await login("utente1", "Password-Stud-1")
    check("2FA: la password da sola non basta", d.get("need_2fa") is True)
    _, st, d = await login("utente1", "Password-Stud-1", "123456" if good != "123456" else "654321")
    check("2FA: codice errato rifiutato", st == 401)
    _, st, _ = await login("utente1", "Password-Stud-1", good)
    check("2FA: codice già usato non vale una seconda volta", st == 401)
    while totp(secret, int(time.time() // 30)) == good:
        await asyncio.sleep(1)
    stud, st, _ = await login("utente1", "Password-Stud-1", totp(secret, int(time.time() // 30)))
    check("2FA: login con codice nuovo", st == 200)

    # ------------------------------------------------ assistente e cronologia per utente
    try:
        async with new_session() as s:
            async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/") as ws:
                pass
        check("WebSocket senza login rifiutato", False)
    except aiohttp.WSServerHandshakeError as e:
        check("WebSocket senza login rifiutato", e.status == 401)
    ans = await ask(stud, "Rispondi solo con la parola ciao.")
    check("Utente loggato usa l'assistente", "ciao" in ans.lower(), ans)
    await ask(admin, "Rispondi solo con la parola salve.")
    today = time.strftime("%Y-%m-%d")
    _, d, _ = await get(stud, f"/api/history?day={today}")
    users_seen = {m.get("user") for m in d["messages"]}
    check("Cronologia: l'utente vede solo le sue conversazioni", users_seen == {"utente1"}, str(users_seen))
    _, d, _ = await get(admin, f"/api/history?day={today}")
    check("Cronologia: l'admin vede tutte", {"utente1", "admin"} <= {m.get("user") for m in d["messages"]})
    async with stud.delete(BASE + f"/api/history?day={today}", headers=H) as r:
        check("Cronologia: l'utente non può cancellare", r.status == 403)

    # ------------------------------------------------ anti-spam domande
    replies = [await ask(stud, f"Rispondi solo con il numero {i}.") for i in range(3)]
    check("Limite domande al minuto per utente", "troppe domande" in replies[-1].lower(), replies[-1][:60])

    # ------------------------------------------------ dispositivi xiaozhi
    dev = {"Device-Id": "AA:BB:CC:11:22:33", "Client-Id": "t"}
    async with new_session() as s:
        async with s.post(BASE + "/xiaozhi/ota/", json={"application": {"version": "1.0"}}, headers=dev) as r:
            o = await r.json()
        check("Dispositivo nuovo: riceve un codice di attivazione, non il WebSocket",
              "activation" in o and "websocket" not in o, o.get("activation", {}).get("code", ""))
        dcode = o["activation"]["code"]
        async with s.post(BASE + "/xiaozhi/ota/activate", json={}, headers=dev) as r:
            check("Attivazione in attesa: 202", r.status == 202)
        try:
            async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/",
                                    headers={**dev, "Authorization": "Bearer inventato"}):
                pass
            check("Dispositivo non approvato: WebSocket rifiutato", False)
        except aiohttp.WSServerHandshakeError as e:
            check("Dispositivo non approvato: WebSocket rifiutato", e.status == 401)
        st, d = await post(admin, "/api/admin/devices", {"code": "999999" if dcode != "999999" else "000000"})
        check("Approvazione con codice sbagliato rifiutata", st == 400)
        st, d = await post(admin, "/api/admin/devices", {"code": dcode, "name": "Robot di prova"})
        check("Admin approva il dispositivo col codice", st == 200)
        async with s.post(BASE + "/xiaozhi/ota/", json={}, headers=dev) as r:
            o = await r.json()
        token = o.get("websocket", {}).get("token")
        check("Dispositivo approvato: riceve WebSocket e token", bool(token) and "activation" not in o)
        async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/",
                                headers={**dev, "Authorization": f"Bearer {token}"}) as ws:
            await ws.send_json({"type": "hello", "transport": "websocket",
                                "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
            hello = json.loads((await ws.receive(timeout=10)).data)
        check("Dispositivo approvato: connesso", hello.get("type") == "hello")

        # --- attacco: conosco il MAC del dispositivo approvato (e perfino il suo token), ma non il Client-Id
        thief = {"Device-Id": dev["Device-Id"], "Client-Id": "ladro-0000"}
        async with s.post(BASE + "/xiaozhi/ota/", json={}, headers=thief) as r:
            o2 = await r.json()
        check("Impersonazione: stesso MAC ma altro Client-Id NON riceve il token",
              "websocket" not in o2 and "activation" in o2, o2.get("activation", {}).get("code", ""))
        check("Impersonazione: riceve un codice diverso da quello del dispositivo vero",
              o2.get("activation", {}).get("code") != dcode)
        try:
            async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/",
                                    headers={**thief, "Authorization": f"Bearer {token}"}):
                pass
            check("Impersonazione: token rubato inutile con un altro Client-Id", False)
        except aiohttp.WSServerHandshakeError as e:
            check("Impersonazione: token rubato inutile con un altro Client-Id", e.status == 401)

        # --- visione: il dispositivo approvato carica una foto col SUO token (prima rispondeva 401)
        form = aiohttp.FormData()
        form.add_field("question", "Cosa vedi?")
        form.add_field("file", b"\xff\xd8\xff\xe0finto", filename="camera.jpg", content_type="image/jpeg")
        async with s.post(BASE + "/xiaozhi/vision/explain", data=form,
                          headers={**dev, "Authorization": f"Bearer {token}"}) as r:
            vj = await r.json()
            check("Visione: dispositivo approvato accettato col suo token", r.status == 200 and vj.get("success"),
                  f"HTTP {r.status}")
        form = aiohttp.FormData()
        form.add_field("file", b"x", filename="c.jpg", content_type="image/jpeg")
        async with s.post(BASE + "/xiaozhi/vision/explain", data=form,
                          headers={**thief, "Authorization": f"Bearer {token}"}) as r:
            check("Visione: rifiutata a chi non è il dispositivo", r.status == 401)

        # --- revoca con il WebSocket già aperto: la connessione deve chiudersi subito
        async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/",
                                headers={**dev, "Authorization": f"Bearer {token}"}) as live:
            await live.send_json({"type": "hello", "transport": "websocket",
                                  "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
            await live.receive(timeout=10)
            async with admin.delete(BASE + "/api/admin/devices", json={"id": dev["Device-Id"].lower() + "|t"}, headers=H) as r:
                pass
            m = await live.receive(timeout=10)
            check("Dispositivo revocato: il WebSocket già aperto viene chiuso subito",
                  m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING), str(m.type))
        try:
            async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/",
                                    headers={**dev, "Authorization": f"Bearer {token}"}):
                pass
            check("Dispositivo revocato: WebSocket rifiutato", False)
        except aiohttp.WSServerHandshakeError as e:
            check("Dispositivo revocato: WebSocket rifiutato", e.status == 401)

    # ------------------------------------------------ strumenti personalizzati e risposte immediate (pagina Admin)
    st, d, _ = await get(admin, "/api/admin/tools")
    check("Strumenti personalizzati: tutti spenti all'inizio, strumenti pronti disponibili",
          st == 200 and not any(d["kinds"].values()) and len(d["presets"]) >= 5)
    st, _ = await post(stud, "/api/admin/tools", {"action": "kind", "kind": "command", "enabled": True})
    check("Strumenti personalizzati: un utente normale non può accenderli", st == 403)
    preset = next(p for p in d["presets"] if p["preset"] == "disco")
    st, d = await post(admin, "/api/admin/tools", {"action": "save", "tool": {**preset, "id": None}})
    tid = next(t["id"] for t in d.get("tools", []) if t["name"] == "spazio_disco") if st == 200 else ""
    st, d = await post(admin, "/api/admin/tools", {"action": "test", "id": tid, "args": {}})
    check("Strumenti personalizzati: l'admin aggiunge e prova un comando già pronto",
          st == 200 and "Spazio libero" in d.get("result", ""), d.get("result", d.get("error", "")))
    st, d = await post(admin, "/api/admin/tools", {"action": "save", "tool": {
        "kind": "command", "name": "pericoloso", "description": "x", "argv": ["{prog}"], "params": [{"name": "prog"}]}})
    check("Strumenti personalizzati: un comando scelto dal modello viene rifiutato", st == 400, d.get("error", ""))
    wiki = next(p for p in (await get(admin, "/api/admin/tools"))[1]["presets"] if p["preset"] == "wikipedia")
    await post(admin, "/api/admin/tools", {"action": "save", "tool": {**wiki, "id": None}})
    await post(admin, "/api/admin/tools", {"action": "kind", "kind": "web", "enabled": True})
    _, s, _ = await get(admin, "/api/admin/status")
    check("Privacy: i siti attivati dall'admin compaiono come esterni",
          any(p.get("opt_in") and "wikipedia" in p["url"] for p in s["privacy"]))
    await post(admin, "/api/admin/tools", {"action": "kind", "kind": "web", "enabled": False})
    st, d = await post(admin, "/api/admin/faq", {"questions": ["A che ora inizia la lezione?"], "answer": "Alle otto e dieci."})
    check("Risposte immediate: l'admin ne aggiunge una", st == 200 and len(d.get("items", [])) == 1)
    st, _ = await post(stud, "/api/admin/faq", {"questions": ["x"], "answer": "y"})
    check("Risposte immediate: un utente normale non può modificarle", st == 403)
    t0 = time.time()
    ans = await ask(admin, "a che ora inizia la lezione")
    check("Risposte immediate: la domanda riceve subito la risposta fissa", "otto e dieci" in ans.lower(),
          f"{ans!r} in {time.time() - t0:.1f}s")
    _, ev, _ = await get(admin, "/api/admin/events")
    kinds_logged = {e["kind"] for e in ev["events"]}
    check("Registro: modifiche a strumenti e risposte immediate registrate",
          {"custom_tools", "faq"} <= kinds_logged, str(sorted(kinds_logged)))

    # ------------------------------------------------ dati cifrati sul disco
    data_dir = LOG.parent
    users_raw = (data_dir / "users.json").read_text(encoding="utf-8")
    check("Segreto 2FA salvato cifrato (non leggibile nel file)", secret not in users_raw and "enc1:" in users_raw)
    hist_raw = "".join(p.read_text(encoding="utf-8") for p in (data_dir / "history").glob("*.jsonl"))
    check("Cronologia salvata cifrata (le domande non si leggono nel file)", "ciao" not in hist_raw.lower() and "enc1:" in hist_raw)

    # ------------------------------------------------ HTTPS
    https = BASE.replace("http://", "https://").replace(":8081", ":8481")
    async with aiohttp.ClientSession() as hs:
        async with hs.get(https + "/login", ssl=False) as r:
            check("HTTPS attivo (certificato creato in automatico)", r.status == 200, https)
        import socket as _s
        lan = _s.gethostbyname(_s.gethostname())
        try:
            u = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            u.connect(("10.255.255.255", 1))
            lan = u.getsockname()[0]
            u.close()
        except OSError:
            pass
        async with hs.get(f"http://{lan}:8081/login", allow_redirects=False) as r:
            check("Da un altro PC l'HTTP rimanda all'HTTPS", r.status == 302 and r.headers.get("Location", "").startswith("https://"),
                  r.headers.get("Location", ""))
        async with hs.post(f"http://{lan}:8081/xiaozhi/ota/", json={}, headers={"Device-Id": "x", "Client-Id": "y"},
                           allow_redirects=False) as r:
            check("I dispositivi restano in HTTP (niente redirect)", r.status == 200)
        async with hs.post(f"http://{lan}:8081/api/login", json={"username": "admin", "password": "x"},
                           headers=H, allow_redirects=False) as r:
            check("Da un altro PC: login in HTTP (password in chiaro) rifiutato", r.status == 426, f"HTTP {r.status}")

    # ------------------------------------------------ sessioni
    async with stud.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/?device-id=web") as live:
        await live.send_json({"type": "hello", "transport": "websocket",
                              "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1}})
        await live.receive(timeout=10)
        st, d = await post(stud, "/api/account/password", {"old": "Password-Stud-1", "new": "Password-Nuova-2"})
        check("Cambio password", st == 200)
        m = await live.receive(timeout=10)
        check("Cambio password: il WebSocket già aperto viene chiuso",
              m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING), str(m.type))
    st, _, _ = await get(stud, "/api/me")
    check("Dopo il cambio password le vecchie sessioni non valgono più", st == 401)
    await post(admin, "/api/logout", {})
    st, _, _ = await get(admin, "/api/me")
    check("Logout: la sessione non vale più", st == 401)

    for s in (anon, stud, s2):
        await s.close()
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\n==== {passed}/{len(RESULTS)} test di sicurezza superati ====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
