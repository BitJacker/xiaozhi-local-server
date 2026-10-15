"""Server HTTP/WebSocket: OTA, WebSocket xiaozhi, visione, client web, account e amministrazione."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import socket
import time
from pathlib import Path

from aiohttp import web

from . import config as config_mod
from . import updater
from .auth import Auth, otpauth_uri
from .engines import ASR, TTS
from .history import History
from .llm import LLM
from .session import Session
from .tools import ExternalMCP

log = logging.getLogger("server")
ROOT = config_mod.ROOT
COOKIE = "husky_session"
LOCAL_ADMIN = {"username": "locale", "role": "admin", "must_setup_2fa": False}  # con auth disattivato


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # nessun pacchetto inviato: serve solo a scegliere l'interfaccia
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def client_ip(request: web.Request) -> str:
    return request.remote or "?"


def legacy_token_ok(request: web.Request) -> bool:
    """Token fissi di config (server.tokens): utili per script e dispositivi configurati a mano."""
    tokens = request.app["cfg"]["server"].get("tokens") or []
    if not tokens:
        return False
    auth = request.headers.get("Authorization", "")
    given = auth.removeprefix("Bearer ").strip() or request.query.get("token", "")
    return any(secrets.compare_digest(given, t) for t in tokens)


def current_user(request: web.Request) -> dict | None:
    auth: Auth = request.app["auth"]
    if not auth.enabled:
        if request.app["cfg"]["server"].get("tokens") and not legacy_token_ok(request):
            return None
        return LOCAL_ADMIN
    return auth.user_from_token(request.cookies.get(COOKIE))


def json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ------------------------------------------------------------------ middleware
PUBLIC = ("/login", "/api/auth/state", "/api/login", "/api/setup", "/health", "/xiaozhi/")
PAGES = {"/": "index.html", "/cronologia": "cronologia.html", "/account": "account.html", "/admin": "admin.html"}


@web.middleware
async def security(request: web.Request, handler):
    app = request.app
    lim = app["cfg"].get("limits") or {}
    path = request.path
    # anti-spam: limite di richieste per IP sulle API e sulle pagine
    if not path.startswith("/xiaozhi/v1") and not app["auth"].limiter.allow(
            f"req:{client_ip(request)}", int(lim.get("requests_per_minute", 240)), 60):
        return json_error("Troppe richieste: rallenta", 429)
    # protezione CSRF: le API che modificano qualcosa richiedono un'intestazione che un altro sito non può inviare
    if request.method in ("POST", "DELETE", "PUT") and path.startswith("/api/") \
            and request.headers.get("X-Requested-With") != "husky":
        return json_error("Richiesta non valida", 403)

    if not path.startswith(PUBLIC):
        user = current_user(request)
        if user is None:
            if path.startswith("/api/"):
                return json_error("Accesso richiesto", 401)
            return web.HTTPFound(f"/login?next={path}")
        if user.get("must_setup_2fa") and not (path.startswith("/account") or path.startswith("/api/account")
                                               or path in ("/api/logout", "/api/me")):
            if path.startswith("/api/"):
                return json_error("Prima configura la verifica in due passaggi (2FA)", 403)
            return web.HTTPFound("/account?setup2fa=1")
        if (path.startswith("/admin") or path.startswith("/api/admin")) and user["role"] != "admin":
            return json_error("Solo per amministratori", 403) if path.startswith("/api/") else web.HTTPFound("/")
        request["user"] = user

    resp = await handler(request)
    if isinstance(resp, web.StreamResponse) and not isinstance(resp, web.WebSocketResponse):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; script-src 'self' 'unsafe-inline' blob:; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                "connect-src 'self' ws: wss:; frame-ancestors 'none'")
    return resp


# ------------------------------------------------------------------ pagine
def page(name: str):
    async def handler(request: web.Request) -> web.FileResponse:
        resp = web.FileResponse(ROOT / "web" / name)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return handler


async def login_page(request: web.Request):
    return await page("login.html")(request)


# ------------------------------------------------------------------ autenticazione
def set_session_cookie(request: web.Request, resp: web.Response, token: str):
    days = float(request.app["cfg"].get("auth", {}).get("session_days", 7))
    resp.set_cookie(COOKIE, token, max_age=int(days * 86400), httponly=True, samesite="Strict",
                    secure=request.secure, path="/")


async def auth_state(request: web.Request):
    auth: Auth = request.app["auth"]
    return web.json_response({"auth_enabled": auth.enabled, "needs_setup": auth.needs_setup(),
                              "user": current_user(request), "version": config_mod.version()})


async def api_setup(request: web.Request):
    """Creazione del primo admin: serve il codice stampato nel terminale del server."""
    auth: Auth = request.app["auth"]
    if not auth.needs_setup():
        return json_error("Il server è già configurato", 403)
    if not auth.limiter.allow(f"setup:{client_ip(request)}", 5, 600):
        return json_error("Troppi tentativi: riprova più tardi", 429)
    d = await body(request)
    if not secrets.compare_digest(str(d.get("code", "")).strip(), auth.setup_code):
        return json_error("Codice di configurazione errato (lo trovi nel terminale del server)", 403)
    err = auth.create_user(d.get("username", ""), d.get("password", ""), "admin")
    if err:
        return json_error(err)
    token, _ = auth.login(d["username"], d["password"], "", client_ip(request))
    resp = web.json_response({"ok": True})
    set_session_cookie(request, resp, token)
    log.info("Creato l'amministratore '%s'", d["username"])
    return resp


async def api_login(request: web.Request):
    auth: Auth = request.app["auth"]
    d = await body(request)
    token, err = auth.login(str(d.get("username", "")), str(d.get("password", "")),
                            str(d.get("code", "")), client_ip(request))
    if err == "2fa":
        return web.json_response({"need_2fa": True})
    if err:
        log.warning("Login fallito per '%s' da %s", d.get("username"), client_ip(request))
        return json_error(err, 429 if "Troppi" in err else 401)
    resp = web.json_response({"ok": True})
    set_session_cookie(request, resp, token)
    return resp


async def api_logout(request: web.Request):
    request.app["auth"].logout(request.cookies.get(COOKIE, ""))
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_me(request: web.Request):
    user = request["user"]
    auth: Auth = request.app["auth"]
    info = dict(user)
    if auth.enabled and user["username"] in auth.users.data:
        info.update(auth.public_user(user["username"]))
    info["auth_enabled"] = auth.enabled
    info["require_2fa"] = bool(request.app["cfg"].get("auth", {}).get("require_2fa"))
    return web.json_response(info)


async def api_account(request: web.Request):
    auth: Auth = request.app["auth"]
    if not auth.enabled:
        return json_error("Gli account sono disattivati (auth.enabled: false)")
    name = request["user"]["username"]
    action = request.match_info["action"]
    d = await body(request)
    from .auth import verify_password
    if action == "password":
        if not verify_password(d.get("old", ""), auth.users.data[name]["password"]):
            return json_error("Password attuale errata", 403)
        err = auth.set_password(name, d.get("new", ""))
        return json_error(err) if err else web.json_response({"ok": True, "relogin": True})
    if action == "2fa-begin":
        import segno
        secret = auth.totp_begin(name)
        uri = otpauth_uri(secret, name)
        return web.json_response({"secret": secret, "uri": uri,
                                  "qr": segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff")})
    if action == "2fa-confirm":
        if not auth.totp_confirm(name, str(d.get("code", ""))):
            return json_error("Codice non valido: controlla l'ora del telefono e riprova")
        return web.json_response({"ok": True})
    if action == "2fa-disable":
        if request.app["cfg"].get("auth", {}).get("require_2fa"):
            return json_error("La 2FA è obbligatoria su questo server")
        if not verify_password(d.get("password", ""), auth.users.data[name]["password"]):
            return json_error("Password errata", 403)
        auth.totp_disable(name)
        return web.json_response({"ok": True})
    return json_error("Azione sconosciuta", 404)


# ------------------------------------------------------------------ amministrazione
async def api_admin(request: web.Request):
    app = request.app
    auth: Auth = app["auth"]
    what = request.match_info["what"]
    d = await body(request) if request.method in ("POST", "DELETE") else {}
    me = request["user"]["username"]

    if what == "status":
        return web.json_response({
            "version": config_mod.version(), "auth_enabled": auth.enabled,
            "users": len(auth.users.data), "sessions": len(auth.sessions.data),
            "devices": len(auth.devices.data), "mcp": {m.name: len(m.tools) for m in app["external_mcp"]},
            "model": app["cfg"]["llm"]["model"], "data_dir": app["cfg"]["_data_dir"],
            "check_on_open": bool((app["cfg"].get("update") or {}).get("check_on_admin_open", True)),
        })
    if what == "users":
        if request.method == "GET":
            return web.json_response({"users": auth.list_users()})
        if request.method == "DELETE":
            if d.get("username") == me:
                return json_error("Non puoi eliminare te stesso")
            err = auth.delete_user(d.get("username", ""))
        elif d.get("action") == "create":
            err = auth.create_user(d.get("username", ""), d.get("password", ""), d.get("role", "user"))
        elif d.get("action") == "password":
            err = auth.set_password(d.get("username", ""), d.get("password", "")) \
                if d.get("username") in auth.users.data else "Utente inesistente"
        elif d.get("action") == "role":
            err = auth.set_role(d.get("username", ""), d.get("role", ""))
        elif d.get("action") == "reset2fa":
            err = None if d.get("username") in auth.users.data else "Utente inesistente"
            if not err:
                auth.totp_disable(d["username"])
        else:
            err = "Azione sconosciuta"
        if err:
            return json_error(err)
        log.info("Admin '%s': %s utente '%s'", me, d.get("action") or request.method, d.get("username"))
        return web.json_response({"ok": True, "users": auth.list_users()})
    if what == "devices":
        if request.method == "GET":
            return web.json_response({"devices": auth.list_devices()})
        if request.method == "DELETE":
            auth.remove_device(d.get("id", ""))
            return web.json_response({"ok": True, "devices": auth.list_devices()})
        err = auth.approve_device(code=str(d.get("code", "")), device_id=d.get("id", ""), name=d.get("name", ""))
        return json_error(err) if err else web.json_response({"ok": True, "devices": auth.list_devices()})
    if what == "update-check":
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, updater.check, app["cfg"])
        except Exception as e:
            return json_error(f"Impossibile contattare GitHub: {e}", 502)
        return web.json_response(info)
    if what == "update-apply":
        if app.get("updating"):
            return json_error("Aggiornamento già in corso", 409)
        app["updating"] = True
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, updater.apply, app["cfg"])
        finally:
            app["updating"] = False
        if res.get("restart"):
            log.warning("Aggiornamento installato: riavvio del server")
            asyncio.get_running_loop().call_later(2, os._exit, updater.RESTART_CODE)
        return web.json_response(res)
    return json_error("Sconosciuto", 404)


# ------------------------------------------------------------------ OTA e dispositivi
async def ota(request: web.Request) -> web.Response:
    """Il dispositivo chiede qui dove collegarsi.

    Con gli account attivi, un dispositivo nuovo riceve un codice di attivazione a 6 cifre (lo mostra
    sul suo schermo): l'admin lo approva nella pagina Admin, come succedeva con xiaozhi.me ma in locale.
    """
    app = request.app
    auth: Auth = app["auth"]
    d = {}
    if request.can_read_body:
        try:
            d = await request.json()
        except Exception:
            d = {}
    current = (d.get("application") or {}).get("version", "1.0.0")
    device_id = request.headers.get("Device-Id", "")
    tz_min = -time.timezone // 60 if not time.localtime().tm_isdst else -time.altzone // 60
    resp = {"server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": tz_min},
            # stessa versione del dispositivo => nessun aggiornamento firmware forzato
            "firmware": {"version": current, "url": ""}}
    if not auth.enabled:
        resp["websocket"] = {"url": app["ws_url"], "token": app["token_for_devices"], "version": 1}
        return web.json_response(resp)
    if not device_id:
        return json_error("Device-Id mancante")
    st = auth.device_status(device_id)
    log.info("OTA da %s (device %s, %s)", client_ip(request), device_id, st["status"])
    if st["status"] == "approved":
        resp["websocket"] = {"url": app["ws_url"], "token": auth.device_token(device_id), "version": 1}
    elif st["status"] == "pending":
        resp["activation"] = {"code": st["code"], "challenge": secrets.token_hex(8), "timeout_ms": 30000,
                              "message": f"Codice {st['code']}: inseriscilo nella pagina Admin del server"}
    else:
        return json_error("Troppi dispositivi in attesa di approvazione", 429)
    return web.json_response(resp)


async def ota_activate(request: web.Request) -> web.Response:
    auth: Auth = request.app["auth"]
    if not auth.enabled:
        return web.json_response({"success": True})
    d = auth.devices.data.get(request.headers.get("Device-Id", "").lower())
    if d and d["status"] == "approved":
        return web.json_response({"success": True})
    return web.json_response({"success": False, "message": "In attesa di approvazione"}, status=202)


def ws_identity(request: web.Request) -> dict | None:
    """Chi sta aprendo il WebSocket: utente loggato (browser), dispositivo approvato o token fisso."""
    auth: Auth = request.app["auth"]
    if legacy_token_ok(request):
        return {"username": request.headers.get("Device-Id") or "token", "role": "user", "kind": "device"}
    if not auth.enabled:
        return None if request.app["cfg"]["server"].get("tokens") else {"username": "locale", "role": "admin"}
    user = auth.user_from_token(request.cookies.get(COOKIE))
    if user and not user.get("must_setup_2fa"):
        return {**user, "kind": "web"}
    did = request.headers.get("Device-Id", "")
    tok = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    dev = auth.device_ok(did, tok) if did else None
    if dev:
        return {"username": dev.get("name") or did, "role": "user", "kind": "device"}
    return None


async def websocket(request: web.Request):
    ident = ws_identity(request)
    if ident is None:
        return web.Response(status=401, text="accesso non autorizzato")
    app = request.app
    ip = client_ip(request)
    max_ws = int((app["cfg"].get("limits") or {}).get("ws_per_ip", 6))
    if app["ws_count"].get(ip, 0) >= max_ws:
        return web.Response(status=429, text="troppe connessioni da questo indirizzo")
    app["ws_count"][ip] = app["ws_count"].get(ip, 0) + 1
    try:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        await Session(app, ws, request, user=ident["username"]).run()
        return ws
    finally:
        app["ws_count"][ip] -= 1


# ------------------------------------------------------------------ visione e cronologia
async def vision(request: web.Request) -> web.Response:
    """Riceve foto dalla telecamera del dispositivo e la descrive con un modello locale."""
    if ws_identity(request) is None:
        return web.json_response({"success": False, "message": "accesso non autorizzato"}, status=401)
    question, image = "", b""
    reader = await request.multipart()
    async for part in reader:
        if part.name == "question":
            question = (await part.text())[:500]
        elif part.name == "file":
            image = await part.read(decode=False)
    if not image:
        return web.json_response({"success": False, "message": "nessuna immagine"}, status=400)
    try:
        text = await request.app["llm"].describe_image(image, question)
    except Exception as e:
        log.warning("Visione fallita: %s", e)
        text = "Non riesco ad analizzare l'immagine."
    return web.json_response({"success": True, "result": text})


async def history_api(request: web.Request) -> web.Response:
    """Cronologia sul disco del server. Gli utenti vedono le proprie conversazioni, gli admin tutte."""
    hist: History = request.app["history"]
    user = request["user"]
    only = None if user["role"] == "admin" else user["username"]
    day = request.query.get("day")
    if request.method == "DELETE":
        if only:
            return json_error("Solo un admin può cancellare la cronologia", 403)
        return web.json_response({"deleted": hist.delete(day or "")})
    if day:
        msgs = hist.read(day)
        if only:
            msgs = [m for m in msgs if m.get("user") == only]
        return web.json_response({"day": day, "messages": msgs})
    return web.json_response({"enabled": hist.enabled, "days": hist.days(), "admin": only is None})


async def health(request: web.Request) -> web.Response:
    app = request.app
    return web.json_response({"ok": True, "version": config_mod.version(),
                              "mcp_esterni": {m.name: len(m.tools) for m in app["external_mcp"]}})


# ------------------------------------------------------------------ avvio
async def on_startup(app: web.Application):
    for m in app["external_mcp"]:
        m.start()
    asyncio.create_task(app["llm"].ensure_model())


def build_app(cfg: dict) -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024, middlewares=[security])
    app["cfg"] = cfg
    scfg = cfg["server"]
    host = lan_ip() if scfg.get("public_host", "auto") == "auto" else scfg["public_host"]
    base = f"{host}:{scfg.get('port', 8000)}"
    app["ws_url"] = f"ws://{base}/xiaozhi/v1/"
    app["vision_url"] = f"http://{base}/xiaozhi/vision/explain" if cfg["llm"].get("vision_model") else None
    app["token_for_devices"] = (scfg.get("tokens") or ["locale"])[0]
    app["ws_count"] = {}
    data_dir = Path(cfg["_data_dir"])
    app["auth"] = Auth(cfg.get("auth") or {}, data_dir)
    updater.ensure_manifest(data_dir)

    log.info("Carico i modelli locali (la prima volta Whisper viene scaricato)...")
    app["asr"] = ASR(cfg.get("asr", {}))
    app["tts"] = TTS(cfg["tts"])
    app["llm"] = LLM(cfg["llm"])
    app["external_mcp"] = [ExternalMCP(m["name"], m["url"]) for m in (cfg.get("mcp_servers") or [])]
    app["history"] = History(cfg.get("history") or {}, data_dir)

    for path, name in PAGES.items():
        app.router.add_get(path, page(name))
    app.router.add_get("/login", login_page)
    app.router.add_get("/health", health)
    app.router.add_get("/api/auth/state", auth_state)
    app.router.add_post("/api/setup", api_setup)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/account/{action}", api_account)
    app.router.add_route("*", "/api/admin/{what}", api_admin)
    app.router.add_route("GET", "/api/history", history_api)
    app.router.add_route("DELETE", "/api/history", history_api)
    for path in ("/xiaozhi/ota/", "/xiaozhi/ota"):
        app.router.add_route("*", path, ota)
    app.router.add_post("/xiaozhi/ota/activate", ota_activate)
    app.router.add_get("/xiaozhi/v1/", websocket)
    app.router.add_get("/xiaozhi/v1", websocket)
    app.router.add_post("/xiaozhi/vision/explain", vision)
    app.on_startup.append(on_startup)
    return app


def main():
    ap = argparse.ArgumentParser(description="Server xiaozhi locale (privacy-first)")
    ap.add_argument("-c", "--config", default=None, help="impostazioni predefinite (default: config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "openai", "faster_whisper", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = config_mod.load(args.config)
    app = build_app(cfg)
    scfg = cfg["server"]
    hostport = app["ws_url"].split("//")[1].split("/")[0]
    print(f"\n  Husky locale v{config_mod.version()}   (dati in {cfg['_data_dir']})")
    print(f"  Client web:   http://{hostport}/")
    print(f"  URL OTA:      http://{hostport}/xiaozhi/ota/")
    print(f"  WebSocket:    {app['ws_url']}")
    if app["auth"].needs_setup():
        print("\n  ┌─────────────────────────────────────────────────────────────┐")
        print(f"  │  PRIMO AVVIO: apri http://{hostport}/login".ljust(64) + "│")
        print(f"  │  e crea l'amministratore con questo codice:  {app['auth'].setup_code}".ljust(64) + "│")
        print("  └─────────────────────────────────────────────────────────────┘")
    print(flush=True)
    web.run_app(app, host=scfg.get("host", "0.0.0.0"), port=int(scfg.get("port", 8000)), print=None)


if __name__ == "__main__":
    main()
