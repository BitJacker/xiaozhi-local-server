"""Server HTTP/WebSocket: OTA, WebSocket xiaozhi, visione, client web."""
from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import socket
import time
from pathlib import Path

import yaml
from aiohttp import web

from .engines import ASR, TTS
from .history import History
from .llm import LLM
from .session import Session
from .tools import ExternalMCP

log = logging.getLogger("server")
ROOT = Path(__file__).resolve().parent.parent


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # nessun pacchetto inviato: serve solo a scegliere l'interfaccia
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def check_token(request: web.Request) -> bool:
    tokens = request.app["cfg"]["server"].get("tokens") or []
    if not tokens:
        return True
    auth = request.headers.get("Authorization", "")
    given = auth.removeprefix("Bearer ").strip() or request.query.get("token", "")
    return any(secrets.compare_digest(given, t) for t in tokens)


# ------------------------------------------------------------------ OTA
async def ota(request: web.Request) -> web.Response:
    """Il dispositivo chiede qui dove collegarsi. Rispondiamo col nostro WebSocket.

    Nessuna attivazione/registrazione: niente numero di telefono, niente account.
    """
    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = {}
    current = (body.get("application") or {}).get("version", "1.0.0")
    tz_min = -time.timezone // 60 if not time.localtime().tm_isdst else -time.altzone // 60
    app = request.app
    log.info("OTA da %s (device %s, firmware %s)", request.remote, request.headers.get("Device-Id"), current)
    return web.json_response({
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": tz_min},
        # stessa versione del dispositivo => nessun aggiornamento firmware forzato
        "firmware": {"version": current, "url": ""},
        "websocket": {"url": app["ws_url"], "token": app["token_for_devices"], "version": 1},
    })


async def ota_activate(request: web.Request) -> web.Response:
    return web.json_response({"success": True})


# ------------------------------------------------------------------ WebSocket
async def websocket(request: web.Request):
    if not check_token(request):
        return web.Response(status=401, text="token non valido")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    await Session(request.app, ws, request).run()
    return ws


# ------------------------------------------------------------------ Visione
async def vision(request: web.Request) -> web.Response:
    """Riceve foto dalla telecamera del dispositivo e la descrive con un modello locale."""
    if not check_token(request):
        return web.json_response({"success": False, "message": "token non valido"}, status=401)
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
    # l'immagine non viene salvata: vive solo in RAM durante la richiesta
    return web.json_response({"success": True, "result": text})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "web" / "index.html")


async def history_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "web" / "cronologia.html")


async def history_api(request: web.Request) -> web.Response:
    """Cronologia salvata sul disco del server: elenco dei giorni o conversazioni di un giorno."""
    if not check_token(request):
        return web.json_response({"error": "token non valido"}, status=401)
    hist: History = request.app["history"]
    day = request.query.get("day")
    if request.method == "DELETE":
        return web.json_response({"deleted": hist.delete(day or "")})
    if day:
        return web.json_response({"day": day, "messages": hist.read(day)})
    return web.json_response({"enabled": hist.enabled, "days": hist.days()})


async def health(request: web.Request) -> web.Response:
    app = request.app
    return web.json_response({
        "ok": True,
        "websocket": app["ws_url"],
        "mcp_esterni": {m.name: len(m.tools) for m in app["external_mcp"]},
    })


# ------------------------------------------------------------------ avvio
async def on_startup(app: web.Application):
    for m in app["external_mcp"]:
        m.start()
    asyncio.create_task(app["llm"].ensure_model())


def build_app(cfg: dict) -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app["cfg"] = cfg
    scfg = cfg["server"]
    host = lan_ip() if scfg.get("public_host", "auto") == "auto" else scfg["public_host"]
    base = f"{host}:{scfg.get('port', 8000)}"
    app["ws_url"] = f"ws://{base}/xiaozhi/v1/"
    app["vision_url"] = f"http://{base}/xiaozhi/vision/explain" if cfg["llm"].get("vision_model") else None
    app["token_for_devices"] = (scfg.get("tokens") or ["locale"])[0]

    log.info("Carico i modelli locali (la prima volta Whisper viene scaricato)...")
    app["asr"] = ASR(cfg.get("asr", {}))
    app["tts"] = TTS(cfg["tts"])
    app["llm"] = LLM(cfg["llm"])
    app["external_mcp"] = [ExternalMCP(m["name"], m["url"]) for m in (cfg.get("mcp_servers") or [])]
    app["history"] = History(cfg.get("history") or {}, ROOT)

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/cronologia", history_page)
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
    ap.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "openai", "faster_whisper", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    app = build_app(cfg)
    scfg = cfg["server"]
    print(f"\n  Client web:   http://{app['ws_url'].split('//')[1].split('/')[0]}/")
    print(f"  URL OTA:      http://{app['ws_url'].split('//')[1].split('/')[0]}/xiaozhi/ota/")
    print(f"  WebSocket:    {app['ws_url']}\n")
    web.run_app(app, host=scfg.get("host", "0.0.0.0"), port=int(scfg.get("port", 8000)), print=None)


if __name__ == "__main__":
    main()
