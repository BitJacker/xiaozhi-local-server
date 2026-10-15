"""Server MCP via SSE scritto a mano (protocollo 2024-11-05), senza SDK.

Simula un'implementazione embedded minimale, come quella di un dispositivo: serve a
verificare che il ponte del server non dipenda dalla stessa libreria MCP.
Porta 3001, endpoint /sse.
"""
import asyncio
import json
import uuid

from aiohttp import web

queues: dict[str, asyncio.Queue] = {}
TOOLS = [
    {"name": "get_recognition_result", "description": "Get current recognition results",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "manage_applications", "description": "List or switch algorithm. operation: list|switch",
     "inputSchema": {"type": "object", "properties": {"operation": {"type": "string"}, "name": {"type": "string"}},
                     "required": ["operation"]}},
]


async def sse(request):
    sid = uuid.uuid4().hex
    q = queues[sid] = asyncio.Queue()
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    await resp.write(f"event: endpoint\ndata: /messages?session_id={sid}\n\n".encode())
    try:
        while True:
            msg = await q.get()
            await resp.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode())
    finally:
        queues.pop(sid, None)
    return resp


async def messages(request):
    q = queues.get(request.query.get("session_id"))
    if q is None:
        return web.Response(status=404)
    m = await request.json()
    mid, method = m.get("id"), m.get("method")
    if mid is None:
        return web.Response(status=202, text="Accepted")  # notifica
    if method == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "HUSKYLENS2-sim", "version": "1.1.6"}}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        name = m["params"]["name"]
        text = ('{"algorithm":"FACE_RECOGNITION","blocks":[{"id":1,"name":"Alex","x":160,"y":120}]}'
                if name == "get_recognition_result" else f"ok {m['params'].get('arguments')}")
        res = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "ping":
        res = {}
    else:
        await q.put({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
        return web.Response(status=202, text="Accepted")
    await q.put({"jsonrpc": "2.0", "id": mid, "result": res})
    return web.Response(status=202, text="Accepted")


app = web.Application()
app.router.add_get("/sse", sse)
app.router.add_post("/messages", messages)
web.run_app(app, host="127.0.0.1", port=3001, print=None)
