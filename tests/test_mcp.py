"""Test dei server MCP aggiunti dalla pagina Admin e dei punti di accesso MCP.

* server in rete locale via Streamable HTTP e programma locale via stdio (permesso solo dopo l'interruttore);
* indirizzi fuori dalla rete locale solo con la conferma "usa Internet" (e compaiono nella tabella Privacy);
* ruoli: uno studente non vede gli strumenti riservati agli admin;
* punto di accesso MCP compatibile con xiaozhi.me: un programma si collega con il WebSocket e il token, esattamente
  come fa mcp_pipe.py (lo riproduciamo qui: inoltra le righe JSON tra il WebSocket e un server MCP stdio).
Serve Ollama acceso (oppure --mock).  Uso:  python tests/test_mcp.py [--mock]
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
from run_all import MOCK, PY, env, make_config, stop, wait_health  # noqa: E402

from xiaozhi_locale.auth import Auth  # noqa: E402

PORT, MCP_HTTP = 8099, 18901
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "tests" / "_data_mcp"
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
        await asyncio.sleep(0.3)
    return None


async def login(user, pw):
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with s.post(BASE + "/api/login", json={"username": user, "password": pw}, headers=H) as r:
        assert r.status == 200, await r.text()
    return s


async def ask_text(s, text: str) -> str:
    async with s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/") as ws:
        await ws.send_str(json.dumps({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}}))
        await ws.send_str(json.dumps({"type": "text", "text": text}))
        said = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 120)
            if msg.type == aiohttp.WSMsgType.TEXT:
                o = json.loads(msg.data)
                if o.get("type") == "tts" and o.get("state") == "sentence_start":
                    said.append(o.get("text", ""))
                if o.get("type") == "tts" and o.get("state") == "stop":
                    break
            elif msg.type != aiohttp.WSMsgType.BINARY:
                break
        return " ".join(said)


async def pipe(url: str):
    """Come mcp_pipe.py: un server MCP stdio collegato al punto di accesso via WebSocket."""
    proc = await asyncio.create_subprocess_exec(PY, "tests/fake_mcp_servers.py", "stdio", cwd=ROOT,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(url)

    async def ws_to_proc():
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                proc.stdin.write(msg.data.encode() + b"\n")
                await proc.stdin.drain()

    async def proc_to_ws():
        while line := await proc.stdout.readline():
            await ws.send_str(line.decode().strip())

    tasks = [asyncio.create_task(ws_to_proc()), asyncio.create_task(proc_to_ws())]

    async def close():
        for t in tasks:
            t.cancel()
        await ws.close()
        await session.close()
        proc.kill()
        await proc.wait()
    return ws, close


async def main_async():
    s = await login("prof", "Password-Prof-123")
    stud = await login("studente", "Password-Stud-123")
    try:
        async def mcp(data=None):
            if data is None:
                async with s.get(BASE + "/api/admin/mcp") as r:
                    return r.status, await r.json()
            async with s.post(BASE + "/api/admin/mcp", json=data, headers=H) as r:
                return r.status, await r.json()

        def server(res, name):
            return next((x for x in res.get("servers", []) if x["name"] == name), None)

        st, res = await mcp()
        check("Partenza: nessun server, programmi locali non permessi", st == 200 and not res["stdio_allowed"]
              and not res["servers"])

        # ---------------- server in rete locale (Streamable HTTP)
        st, res = await mcp({"action": "save_server", "name": "laboratorio", "url": f"http://127.0.0.1:{MCP_HTTP}/mcp",
                             "roles": ["admin", "user"]})
        lab = server(res, "laboratorio") or {}
        check("Server HTTP aggiunto: riconosciuto come Streamable HTTP e SPENTO", st == 200 and lab.get("transport") == "http"
              and not lab.get("enabled"), str(lab)[:120])
        await mcp({"action": "toggle_server", "id": lab["id"], "enabled": True})
        ok = await until(lambda: mcp_ready(s, "laboratorio"), 20)
        check("Server HTTP acceso: collegato con i suoi strumenti", ok, str(ok)[:120])

        # ---------------- programma locale (stdio): solo dopo l'interruttore
        prog = {"action": "save_server", "name": "programma", "command": PY,
                "args": "tests/fake_mcp_servers.py\nstdio", "roles": ["admin"]}
        st, res = await mcp(prog)
        check("Programma MCP locale rifiutato finché non sono permessi", st == 400, res.get("error", ""))
        await mcp({"action": "stdio", "enabled": True})
        st, res = await mcp(prog)
        p = server(res, "programma") or {}
        await mcp({"action": "toggle_server", "id": p.get("id"), "enabled": True})
        ok = await until(lambda: mcp_ready(s, "programma"), 30)
        check("Programma MCP locale (stdio) avviato e collegato", st == 200 and p.get("transport") == "stdio" and ok)

        # ---------------- nomi e indirizzi
        st1, r1 = await mcp({"action": "save_server", "name": "Lab!", "url": "http://127.0.0.1:1/mcp", "roles": ["admin"]})
        st2, _ = await mcp({"action": "save_server", "name": "laboratorio", "url": "http://127.0.0.1:1/mcp", "roles": ["admin"]})
        st3, r3 = await mcp({"action": "save_server", "name": "remoto", "url": "https://example.com/mcp", "roles": ["admin"]})
        check("Nome non valido o già usato: rifiutato", st1 == 400 and st2 == 400, r1.get("error", ""))
        check("Indirizzo su Internet senza conferma: rifiutato", st3 == 400 and "rete locale" in r3.get("error", ""))
        st4, r4 = await mcp({"action": "save_server", "name": "remoto", "url": "https://example.com/mcp", "roles": ["admin"],
                             "internet": True})
        async with s.get(BASE + "/api/admin/status") as r:
            priv = (await r.json())["privacy"]
        check("Con la conferma: salvato SPENTO e nella tabella Privacy",
              st4 == 200 and not server(r4, "remoto")["enabled"]
              and any("remoto" in x["what"] and not x["local"] and x.get("opt_in") for x in priv))
        await mcp({"action": "delete_server", "id": server(r4, "remoto")["id"]})

        # ---------------- strumenti nella conversazione, con i ruoli
        a = await ask_text(s, "Usa lo strumento laboratorio_somma con a=2 b=3")
        check("Admin: strumento del server HTTP usato nella conversazione", "5" in a, a[:80])
        a = await ask_text(s, "Usa lo strumento programma_somma con a=40 b=4")
        check("Admin: strumento del programma stdio usato", "44" in a, a[:80])
        a = await ask_text(stud, "Usa lo strumento programma_saluto con nome=Ada")
        check("Studente: lo strumento riservato agli admin non è disponibile", "da stdio" not in a, a[:80])
        a = await ask_text(stud, "Usa lo strumento laboratorio_somma con a=30 b=7")
        check("Studente: lo strumento permesso agli utenti funziona", "37" in a, a[:80])

        # ---------------- punto di accesso MCP (come xiaozhi.me + mcp_pipe.py)
        st, res = await mcp({"action": "save_endpoint", "name": "pc_studente", "roles": ["admin", "user"]})
        ep = next(e for e in res["endpoints"] if e["name"] == "pc_studente")
        check("Punto di accesso creato con indirizzo ws://.../xiaozhi/mcp/?token=",
              st == 200 and "/xiaozhi/mcp/?token=" in ep["url"] and len(ep["url"].split("token=")[1]) > 20)
        url = BASE.replace("http", "ws") + "/xiaozhi/mcp/?token=" + ep["url"].split("token=")[1]
        try:
            async with aiohttp.ClientSession() as anon:
                await anon.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/mcp/?token=sbagliato")
            bad = None
        except aiohttp.WSServerHandshakeError as e:
            bad = e.status
        check("Punto di accesso: token sbagliato rifiutato (401)", bad == 401, str(bad))
        ws, close = await pipe(url)
        ok = await until(lambda: endpoint_ready(s, "pc_studente"), 20)
        check("Programma collegato al punto di accesso: strumenti visibili", ok, str(ok)[:100])
        a = await ask_text(stud, "Usa lo strumento pc_studente_somma con a=50 b=8")
        check("Strumento del punto di accesso usato nella conversazione", "58" in a, a[:80])
        await mcp({"action": "delete_endpoint", "id": ep["id"]})
        check("Punto di accesso eliminato: il programma viene scollegato", await until(lambda: ws.closed, 10))
        await close()

        # ---------------- spento: gli strumenti spariscono
        await mcp({"action": "toggle_server", "id": lab["id"], "enabled": False})
        a = await ask_text(s, "Usa lo strumento laboratorio_somma con a=2 b=3")
        check("Server spento: i suoi strumenti non ci sono più", "calcolato" not in a, a[:80])
        async with s.get(BASE + "/api/admin/events") as r:
            evs = (await r.json())["events"]
        check("Registro di sicurezza: modifiche ai server MCP registrate", sum(1 for e in evs if e.get("kind") == "mcp") >= 5)
    finally:
        await s.close()
        await stud.close()


async def mcp_ready(s, name):
    async with s.get(BASE + "/api/admin/mcp") as r:
        x = next((x for x in (await r.json())["servers"] if x["name"] == name), None)
    return x if x and x["connected"] and x["tools"] else None


async def endpoint_ready(s, name):
    async with s.get(BASE + "/api/admin/mcp") as r:
        x = next((x for x in (await r.json())["endpoints"] if x["name"] == name), None)
    return x if x and x["connected"] and x["tools"] else None


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    a = Auth({"enabled": True}, DATA)
    a.create_user("prof", "Password-Prof-123", "admin")
    a.create_user("studente", "Password-Stud-123", "user")
    cfg = make_config(PORT, "tests/_data_mcp", auth=True, turns=100, huskylens=False)
    cfg = cfg.replace("tool_groups_threshold: 12", "tool_groups_threshold: 0")
    (ROOT / "tests" / "config.mcp.yaml").write_text(cfg, encoding="utf-8")
    procs = [subprocess.Popen([PY, "tests/fake_mcp_servers.py", "http", str(MCP_HTTP)], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    if MOCK and not _port_open(1299):
        procs.append(subprocess.Popen([PY, "tests/mock_llm.py"], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    log = open(DATA / "server.log", "w", encoding="utf-8")
    procs.append(subprocess.Popen([PY, "server.py", "-c", "tests/config.mcp.yaml"], cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT))
    try:
        if not wait_health(BASE + "/health", '"ok"'):
            print("server non partito:\n" + (DATA / "server.log").read_text(encoding="utf-8")[-2000:])
            return 1
        asyncio.run(main_async())
    finally:
        stop(procs)
        log.close()
    if all(RESULTS):
        shutil.rmtree(DATA, ignore_errors=True)
    (ROOT / "tests" / "config.mcp.yaml").unlink(missing_ok=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test MCP superati ====")
    return 0 if all(RESULTS) else 1


def _port_open(port: int) -> bool:
    with socket.socket() as so:
        so.settimeout(0.3)
        return so.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    sys.exit(main())
