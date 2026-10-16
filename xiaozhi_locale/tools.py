"""Strumenti (tool) MCP disponibili per l'LLM.

Due sorgenti:
  * il dispositivo xiaozhi stesso (MCP incapsulato nei messaggi WebSocket "type: mcp");
  * server MCP esterni in LAN, es. HuskyLens 2 (trasporto SSE, http://IP:3000/sse).
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import logging
import re
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

log = logging.getLogger("tools")

CallFn = Callable[[str, dict], Awaitable[str]]
DescribeFn = Callable[[bytes, str], Awaitable[str]]  # (immagine, domanda) -> descrizione (modello visivo locale)


def _safe_name(name: str) -> str:
    """I nomi delle funzioni OpenAI ammettono solo [a-zA-Z0-9_-] (max 64)."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


class ToolSet:
    """Raccolta di tool esposta all'LLM in formato OpenAI function-calling."""

    def __init__(self):
        self._tools: dict[str, tuple[dict, str, CallFn]] = {}

    def add(self, name: str, description: str, schema: dict | None, call: CallFn, prefix: str = ""):
        safe = _safe_name(f"{prefix}{name}")
        spec = {
            "type": "function",
            "function": {
                "name": safe,
                "description": (description or name)[:1000],
                "parameters": schema or {"type": "object", "properties": {}},
            },
        }
        self._tools[safe] = (spec, name, call)

    def merge(self, other: "ToolSet"):
        self._tools.update(other._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def filtered(self, patterns: list[str]) -> "ToolSet":
        """Solo gli strumenti permessi (nomi con * come jolly, senza distinguere maiuscole)."""
        out = ToolSet()
        pats = [p.lower() for p in (patterns or [])]
        for safe, t in self._tools.items():
            names = (safe.lower(), t[1].lower())
            if any(fnmatch.fnmatchcase(n, p) for p in pats for n in names):
                out._tools[safe] = t
        return out

    def specs(self) -> list[dict]:
        return [t[0] for t in self._tools.values()]

    def __len__(self):
        return len(self._tools)

    async def call(self, safe_name: str, arguments: dict) -> str:
        if safe_name not in self._tools:
            return f"Errore: strumento sconosciuto {safe_name}"
        spec, real, fn = self._tools[safe_name]
        params = spec["function"].get("parameters") or {}
        props = params.get("properties")
        if isinstance(props, dict) and props and params.get("additionalProperties") is not True:
            # i modelli piccoli a volte aggiungono argomenti inventati: si passano solo quelli previsti
            extra = [k for k in (arguments or {}) if k not in props]
            if extra:
                log.info("Tool %s: ignoro argomenti non previsti %s", real, extra)
                arguments = {k: v for k, v in arguments.items() if k in props}
        try:
            return await asyncio.wait_for(fn(real, arguments), timeout=30)
        except asyncio.TimeoutError:
            return "Errore: lo strumento non ha risposto in tempo"
        except Exception as e:
            log.exception("tool %s fallito", real)
            return f"Errore: {e}"


def _field(c: Any, name: str):
    return c.get(name) if isinstance(c, dict) else getattr(c, name, None)


async def content_to_text(content: Any, describe: DescribeFn | None = None, tool: str = "") -> str:
    """Converte il risultato MCP (lista di blocchi) in testo per l'LLM.

    Le immagini (es. una foto scattata dalla HuskyLens) vengono descritte dal modello visivo locale,
    se configurato (llm.vision_model); altrimenti l'LLM riceve un avviso chiaro.
    """
    parts = []
    for c in content or []:
        t, text = _field(c, "type"), _field(c, "text")
        if t == "text" and text is not None:
            parts.append(text)
        elif t == "image":
            data = _field(c, "data") or ""
            if describe and data:
                try:
                    desc = await describe(base64.b64decode(data),
                                          "Descrivi in modo preciso cosa si vede in questa immagine della telecamera.")
                    parts.append(f"[Descrizione dell'immagine restituita da {tool or 'uno strumento'}]: {desc}")
                except Exception as e:
                    log.warning("Descrizione immagine fallita: %s", e)
                    parts.append("[immagine ricevuta, ma il modello visivo non è riuscito a descriverla]")
            else:
                parts.append("[immagine ricevuta: per descriverla serve un modello visivo locale (llm.vision_model)]")
        else:
            parts.append(json.dumps(c if isinstance(c, dict) else str(c), ensure_ascii=False))
    return "\n".join(parts) or "ok"


class DeviceMCP:
    """Client MCP verso il dispositivo, sopra il WebSocket xiaozhi."""

    def __init__(self, send_json: Callable[[dict], Awaitable[None]], session_id: str,
                 describe: DescribeFn | None = None):
        self._describe = describe
        self._send = send_json
        self._session_id = session_id
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self.tools = ToolSet()

    async def _request(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        await self._send({"session_id": self._session_id, "type": "mcp", "payload": payload})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def handle(self, payload: dict):
        """Chiamato per ogni messaggio "mcp" ricevuto dal dispositivo."""
        rid = payload.get("id")
        if rid in self._pending and not self._pending[rid].done():
            if "error" in payload:
                self._pending[rid].set_exception(RuntimeError(payload["error"].get("message", "errore MCP")))
            else:
                self._pending[rid].set_result(payload.get("result", {}))

    async def initialize(self, vision_url: str | None, token: str):
        caps: dict = {}
        if vision_url:
            caps["vision"] = {"url": vision_url, "token": token}
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": caps,
            "clientInfo": {"name": "xiaozhi-locale", "version": "1.0"},
        })
        cursor = ""
        while True:
            res = await self._request("tools/list", {"cursor": cursor})
            for t in res.get("tools", []):
                self.tools.add(t["name"], t.get("description", ""), t.get("inputSchema"), self._call)
            cursor = res.get("nextCursor") or ""
            if not cursor:
                break
        log.info("Dispositivo: %d tool MCP disponibili", len(self.tools))

    async def _call(self, name: str, args: dict) -> str:
        res = await self._request("tools/call", {"name": name, "arguments": args}, timeout=30)
        return await content_to_text(res.get("content"), self._describe, name)


class ExternalMCP:
    """Connessione persistente a un server MCP in LAN (es. HuskyLens 2) via SSE.

    Si accorge in pochi secondi se la telecamera si spegne o il Wi-Fi cade (ping ogni 5 s) e si ricollega
    da sola. Se una chiamata trova la connessione appena caduta, aspetta la riconnessione e riprova una volta.
    """

    PING_EVERY = 5

    def __init__(self, name: str, url: str, describe: DescribeFn | None = None):
        self._describe = describe
        self.name = name
        self.url = url
        self.tools = ToolSet()
        self._session = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()   # connesso e strumenti pronti
        self._broken = asyncio.Event()  # una chiamata ha trovato la connessione caduta

    def start(self):
        self._task = asyncio.create_task(self._run())

    def _set_down(self):
        self._session, self.tools = None, ToolSet()
        self._ready.clear()

    async def _run(self):
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        failures = 0
        while True:
            try:
                async with AsyncExitStack() as stack:
                    read, write = await stack.enter_async_context(sse_client(self.url))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=10)
                    ts = ToolSet()
                    for t in listed.tools:
                        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                        ts.add(t.name, t.description or "", schema, self._call, prefix=f"{self.name}_")
                    self._session, self.tools = session, ts
                    self._broken.clear()
                    self._ready.set()
                    log.info("MCP '%s' connesso: %d tool", self.name, len(ts))
                    failures = 0
                    while True:  # controllo della connessione
                        try:
                            await asyncio.wait_for(self._broken.wait(), timeout=self.PING_EVERY)
                            raise ConnectionError("connessione caduta durante una chiamata")
                        except asyncio.TimeoutError:
                            await asyncio.wait_for(session.send_ping(), timeout=self.PING_EVERY)
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # include ExceptionGroup di anyio
                while isinstance(e, BaseExceptionGroup) and e.exceptions:
                    e = e.exceptions[0]
                log.debug("dettaglio errore MCP", exc_info=e)
                self._set_down()
                # telecamera spenta o Wi-Fi caduto: si riprova presto, poi sempre meno spesso (max 15 s)
                failures += 1
                delay = min(15, 2 * failures) if failures > 1 else 1
                log.warning("MCP '%s' (%s) non raggiungibile: %s. Riprovo tra %d s",
                            self.name, self.url, e or type(e).__name__, delay)
                await asyncio.sleep(delay)
                continue
            self._set_down()
            await asyncio.sleep(1)

    async def _call(self, name: str, args: dict) -> str:
        for attempt in (1, 2):
            if self._session is None:
                try:  # appena ricollegata? aspetta qualche secondo
                    await asyncio.wait_for(self._ready.wait(), timeout=8)
                except asyncio.TimeoutError:
                    return f"Errore: {self.name} non è raggiungibile (telecamera spenta o fuori rete)"
            session = self._session
            try:
                res = await session.call_tool(name, args)
                return await content_to_text(getattr(res, "content", None), self._describe, name)
            except Exception as e:
                if attempt == 2:
                    raise
                log.warning("MCP '%s': chiamata %s fallita (%s), mi ricollego e riprovo", self.name, name, e)
                if self._session is session:
                    self._set_down()
                    self._broken.set()
        return "Errore"
