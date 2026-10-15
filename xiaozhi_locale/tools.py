"""Strumenti (tool) MCP disponibili per l'LLM.

Due sorgenti:
  * il dispositivo xiaozhi stesso (MCP incapsulato nei messaggi WebSocket "type: mcp");
  * server MCP esterni in LAN, es. HuskyLens 2 (trasporto SSE, http://IP:3000/sse).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

log = logging.getLogger("tools")

CallFn = Callable[[str, dict], Awaitable[str]]


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

    def specs(self) -> list[dict]:
        return [t[0] for t in self._tools.values()]

    def __len__(self):
        return len(self._tools)

    async def call(self, safe_name: str, arguments: dict) -> str:
        if safe_name not in self._tools:
            return f"Errore: strumento sconosciuto {safe_name}"
        _, real, fn = self._tools[safe_name]
        try:
            return await asyncio.wait_for(fn(real, arguments), timeout=30)
        except asyncio.TimeoutError:
            return "Errore: lo strumento non ha risposto in tempo"
        except Exception as e:
            log.exception("tool %s fallito", real)
            return f"Errore: {e}"


def _content_to_text(content: Any) -> str:
    """Converte il risultato MCP (lista di blocchi) in testo per l'LLM."""
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            t, text = c.get("type"), c.get("text")
        else:
            t, text = getattr(c, "type", None), getattr(c, "text", None)
        if t == "text" and text is not None:
            parts.append(text)
        elif t == "image":
            parts.append("[immagine ricevuta]")
        else:
            parts.append(json.dumps(c if isinstance(c, dict) else str(c), ensure_ascii=False))
    return "\n".join(parts) or "ok"


class DeviceMCP:
    """Client MCP verso il dispositivo, sopra il WebSocket xiaozhi."""

    def __init__(self, send_json: Callable[[dict], Awaitable[None]], session_id: str):
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
        return _content_to_text(res.get("content"))


class ExternalMCP:
    """Connessione persistente a un server MCP in LAN (es. HuskyLens 2) via SSE."""

    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url
        self.tools = ToolSet()
        self._session = None
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        while True:
            try:
                async with AsyncExitStack() as stack:
                    read, write = await stack.enter_async_context(sse_client(self.url))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listed = await session.list_tools()
                    ts = ToolSet()
                    for t in listed.tools:
                        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
                        ts.add(t.name, t.description or "", schema, self._call, prefix=f"{self.name}_")
                    self._session, self.tools = session, ts
                    log.info("MCP '%s' connesso: %d tool", self.name, len(ts))
                    while True:  # keep-alive
                        await asyncio.sleep(30)
                        await session.send_ping()
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # include ExceptionGroup di anyio
                while isinstance(e, BaseExceptionGroup) and e.exceptions:
                    e = e.exceptions[0]
                log.debug("dettaglio errore MCP", exc_info=e)
                log.warning("MCP '%s' (%s) non raggiungibile: %s. Riprovo tra 15 s", self.name, self.url, e)
            self._session, self.tools = None, ToolSet()
            await asyncio.sleep(15)

    async def _call(self, name: str, args: dict) -> str:
        if self._session is None:
            return f"Errore: {self.name} non è connesso"
        res = await self._session.call_tool(name, args)
        return _content_to_text(getattr(res, "content", None))
