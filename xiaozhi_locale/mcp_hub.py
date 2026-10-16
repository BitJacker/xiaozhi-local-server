"""Server MCP aggiunti dalla pagina Admin e "punti di accesso MCP".

* **Server MCP** (qualsiasi, non solo la HuskyLens): in rete locale via SSE o Streamable HTTP, oppure un programma su
  questo computer (stdio). Partono SPENTI; i programmi locali si possono aggiungere solo dopo averli permessi
  (interruttore apposito), perché girano con i permessi del server. Indirizzi fuori dalla rete locale solo se
  l'admin lo conferma: compaiono nella tabella Privacy.
* **Punto di accesso MCP** (compatibile con xiaozhi.me): un programma che gira su un altro computer (es. il PC di uno
  studente con i suoi strumenti) si collega lui a Husky, con un WebSocket ws://SERVER:8000/xiaozhi/mcp/?token=...
  Funziona con mcp_pipe.py del progetto xiaozhi (variabile MCP_ENDPOINT). Gli strumenti compaiono finché resta
  collegato.

Ogni server e ogni punto di accesso ha i suoi ruoli: chi può usare i suoi strumenti.
Impostazioni in data/mcp.json (token dei punti di accesso cifrati).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from . import privacy
from .auth import JsonStore
from .tools import ExternalMCP, ToolSet, content_to_text, mcp_transport

log = logging.getLogger("mcp_hub")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,29}$")
ROLES = ("admin", "user", "device")


class EndpointMCP:
    """Client MCP sopra un WebSocket aperto DAL programma che offre gli strumenti (come xiaozhi.me)."""

    def __init__(self, name: str, ws: web.WebSocketResponse, describe=None):
        self.name = name
        self.ws = ws
        self._describe = describe
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self.tools = ToolSet()
        self.server_info = ""

    async def _request(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def handle(self, raw: str):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        rid = msg.get("id")
        if "method" in msg:  # richieste del programma (es. ping): si risponde senza fare niente
            if rid is not None and msg["method"] == "ping":
                asyncio.ensure_future(self.ws.send_str(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}})))
            return
        fut = self._pending.get(rid)
        if fut and not fut.done():
            if "error" in msg:
                fut.set_exception(RuntimeError((msg["error"] or {}).get("message", "errore MCP")))
            else:
                fut.set_result(msg.get("result") or {})

    async def initialize(self):
        res = await self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                 "clientInfo": {"name": "husky-locale", "version": "1.0"}})
        self.server_info = str((res.get("serverInfo") or {}).get("name", ""))[:60]
        await self.ws.send_str(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        ts, cursor = ToolSet(), ""
        for _ in range(20):
            res = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            for t in res.get("tools", [])[:200]:
                if isinstance(t, dict) and t.get("name"):
                    ts.add(str(t["name"]), str(t.get("description", "")), t.get("inputSchema"), self._call,
                           prefix=f"{self.name}_")
            cursor = res.get("nextCursor") or ""
            if not cursor:
                break
        self.tools = ts

    async def _call(self, name: str, args: dict) -> str:
        res = await self._request("tools/call", {"name": name, "arguments": args}, timeout=30)
        return await content_to_text(res.get("content"), self._describe, name)


class McpHub:
    def __init__(self, data_dir: Path, box=None, describe=None, allow_external: bool = False):
        self.store = JsonStore(Path(data_dir) / "mcp.json")
        for k, v in (("servers", []), ("endpoints", []), ("stdio_allowed", False)):
            self.store.data.setdefault(k, v)
        self.box = box
        self.describe = describe
        self.allow_external = allow_external
        self.running: dict[str, ExternalMCP] = {}     # server attivi, per id
        self.connected: dict[str, EndpointMCP] = {}   # punti di accesso collegati, per id
        self.reserved: set[str] = set()               # nomi già usati (server di config.yaml)

    # ------------------------------------------------------------------ server
    async def start(self):
        for s in self.store.data["servers"]:
            if s.get("enabled"):
                self._start(s)

    async def stop(self):
        for m in list(self.running.values()):
            await m.stop()
        self.running.clear()
        for ep in list(self.connected.values()):
            await ep.ws.close()

    def _start(self, s: dict):
        if s["transport"] == "stdio" and not self.store.data.get("stdio_allowed"):
            return
        m = ExternalMCP(s["name"], s.get("url", ""), self.describe, s["transport"], s.get("command", ""), s.get("args"))
        m.start()
        self.running[s["id"]] = m

    def _names(self, skip_id: str = "") -> set[str]:
        return self.reserved | {x["name"] for x in self.store.data["servers"] + self.store.data["endpoints"]
                                if x["id"] != skip_id}

    def _roles(self, d: dict) -> list[str]:
        roles = [r for r in d.get("roles") or [] if r in ROLES]
        if not roles:
            raise ValueError("Scegli almeno un ruolo che può usare gli strumenti")
        return roles

    async def save_server(self, d: dict) -> dict:
        sid = str(d.get("id") or secrets.token_hex(4))
        old = next((s for s in self.store.data["servers"] if s["id"] == sid), None)
        name = str(d.get("name", "")).strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("Nome non valido: lettere minuscole, numeri e _ (2-30 caratteri), es. stazione_meteo")
        if name in self._names(sid):
            raise ValueError("Nome già usato da un altro server MCP")
        url, command = str(d.get("url", "")).strip(), str(d.get("command", "")).strip()
        transport = mcp_transport(url, command, str(d.get("transport", "auto")))
        s = {"id": sid, "name": name, "transport": transport, "roles": self._roles(d),
             "enabled": bool(old and old.get("enabled")), "note": str(d.get("note", ""))[:200]}
        if transport == "stdio":
            if not self.store.data.get("stdio_allowed"):
                raise ValueError("Prima permetti i programmi MCP locali (interruttore in alto)")
            if not command or len(command) > 300 or "\n" in command:
                raise ValueError("Scrivi il programma da avviare (es. python oppure npx)")
            args = d.get("args")
            if isinstance(args, str):  # un argomento per riga: niente shell, niente virgolette da interpretare
                args = [a.strip() for a in args.splitlines() if a.strip()]
            if not isinstance(args, list) or len(args) > 30 or any(len(str(a)) > 300 for a in args):
                raise ValueError("Argomenti non validi (uno per riga, al massimo 30)")
            s.update(command=command, args=[str(a) for a in args])
        else:
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError("Indirizzo non valido: deve iniziare con http:// o https://")
            local = privacy.host_is_local(p.hostname)
            if not local and not d.get("internet") and not self.allow_external:
                raise ValueError("Questo indirizzo è fuori dalla rete locale: conferma che i dati possono uscire "
                                 "(casella \"usa Internet\")")
            s.update(url=url, internet=not local)
        with self.store.lock:
            self.store.data["servers"] = [x for x in self.store.data["servers"] if x["id"] != sid] + [s]
            self.store.save()
        if sid in self.running:
            await self.running.pop(sid).stop()
        if s["enabled"]:
            self._start(s)
        return s

    async def set_server_enabled(self, sid: str, on: bool):
        s = next((x for x in self.store.data["servers"] if x["id"] == sid), None)
        if s is None:
            raise ValueError("Server inesistente")
        if on and s["transport"] == "stdio" and not self.store.data.get("stdio_allowed"):
            raise ValueError("Prima permetti i programmi MCP locali")
        with self.store.lock:
            s["enabled"] = bool(on)
            self.store.save()
        if sid in self.running:
            await self.running.pop(sid).stop()
        if on:
            self._start(s)

    async def delete_server(self, sid: str) -> bool:
        before = len(self.store.data["servers"])
        with self.store.lock:
            self.store.data["servers"] = [x for x in self.store.data["servers"] if x["id"] != sid]
            self.store.save()
        if sid in self.running:
            await self.running.pop(sid).stop()
        return len(self.store.data["servers"]) < before

    async def set_stdio_allowed(self, on: bool):
        with self.store.lock:
            self.store.data["stdio_allowed"] = bool(on)
            self.store.save()
        for s in self.store.data["servers"]:
            if s["transport"] == "stdio":
                if s["id"] in self.running:
                    await self.running.pop(s["id"]).stop()
                if on and s.get("enabled"):
                    self._start(s)

    # ------------------------------------------------------------------ punti di accesso
    def save_endpoint(self, d: dict) -> dict:
        eid = str(d.get("id") or secrets.token_hex(4))
        old = next((e for e in self.store.data["endpoints"] if e["id"] == eid), None)
        name = str(d.get("name", "")).strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("Nome non valido: lettere minuscole, numeri e _ (2-30 caratteri), es. pc_laboratorio")
        if name in self._names(eid):
            raise ValueError("Nome già usato da un altro server MCP")
        token = secrets.token_urlsafe(24)
        e = {"id": eid, "name": name, "roles": self._roles(d), "enabled": True,
             "token": old["token"] if old else (self.box.encrypt(token) if self.box else token)}
        with self.store.lock:
            self.store.data["endpoints"] = [x for x in self.store.data["endpoints"] if x["id"] != eid] + [e]
            self.store.save()
        return e

    async def delete_endpoint(self, eid: str) -> bool:
        before = len(self.store.data["endpoints"])
        with self.store.lock:
            self.store.data["endpoints"] = [x for x in self.store.data["endpoints"] if x["id"] != eid]
            self.store.save()
        ep = self.connected.pop(eid, None)
        if ep:
            await ep.ws.close(code=1008, message=b"punto di accesso eliminato")
        return len(self.store.data["endpoints"]) < before

    def _token(self, e: dict) -> str:
        return self.box.decrypt(e["token"]) if self.box else e["token"]

    def endpoint_for_token(self, token: str) -> dict | None:
        for e in self.store.data["endpoints"]:
            if token and secrets.compare_digest(token, self._token(e)):
                return e
        return None

    async def serve_endpoint(self, request: web.Request, e: dict) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        old = self.connected.pop(e["id"], None)
        if old:  # un solo programma per punto di accesso: il nuovo sostituisce il vecchio
            await old.ws.close(code=1000, message=b"sostituito da una nuova connessione")
        ep = EndpointMCP(e["name"], ws, self.describe)
        self.connected[e["id"]] = ep

        async def init():
            try:
                await ep.initialize()
                log.info("Punto di accesso MCP '%s' collegato da %s: %d strumenti", e["name"], request.remote,
                         len(ep.tools))
            except Exception as ex:
                log.warning("Punto di accesso MCP '%s': inizializzazione fallita (%s)", e["name"], ex)
        task = asyncio.create_task(init())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    ep.handle(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            task.cancel()
            if self.connected.get(e["id"]) is ep:
                del self.connected[e["id"]]
            log.info("Punto di accesso MCP '%s' scollegato", e["name"])
        return ws

    # ------------------------------------------------------------------ per la Session e la pagina Admin
    def toolsets_for(self, role: str) -> list[tuple[str, ToolSet]]:
        out = []
        for s in self.store.data["servers"]:
            m = self.running.get(s["id"])
            if m is not None and role in s.get("roles", []):
                out.append((s["name"], m.tools))
        for e in self.store.data["endpoints"]:
            ep = self.connected.get(e["id"])
            if ep is not None and role in e.get("roles", []):
                out.append((e["name"], ep.tools))
        return out

    def external_hosts(self) -> list[dict]:
        return [{"what": f"Server MCP '{s['name']}'", "url": urlparse(s["url"]).netloc}
                for s in self.store.data["servers"] if s.get("url") and s.get("internet")]

    def status(self, base_ws: str = "") -> dict:
        servers = []
        for s in self.store.data["servers"]:
            m = self.running.get(s["id"])
            servers.append({**s, "connected": bool(m and m.connected), "tools": m.tools.names() if m else [],
                            "error": m.last_error if m else ""})
        endpoints = []
        for e in self.store.data["endpoints"]:
            ep = self.connected.get(e["id"])
            endpoints.append({"id": e["id"], "name": e["name"], "roles": e["roles"],
                              "url": f"{base_ws}?token={self._token(e)}", "connected": ep is not None,
                              "tools": ep.tools.names() if ep else [], "program": ep.server_info if ep else ""})
        return {"stdio_allowed": bool(self.store.data.get("stdio_allowed")), "servers": servers,
                "endpoints": endpoints}
