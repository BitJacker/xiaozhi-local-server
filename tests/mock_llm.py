"""LLM finto compatibile OpenAI, solo per i test (porta 1299). Supporta anche le risposte in streaming."""
import json
import re
import time

from aiohttp import web


def answer(body):
    msgs, tools = body["messages"], body.get("tools") or []
    last = msgs[-1]
    content = last["content"] if isinstance(last.get("content"), str) else ""
    msg = {"role": "assistant", "content": ""}
    if last["role"] == "user" and "Dal messaggio dell'utente estrai" in content:
        return {"role": "assistant", "content": "NESSUNA"}  # estrazione dei ricordi (memoria)
    if last["role"] == "user" and "TRASCRIZIONE" in content or "Punti chiave" in content:
        # riassunto della modalità lezione
        return {"role": "assistant", "content": "Argomento: prova della modalità lezione\nPunti chiave:\n- fotosintesi\n"
                                                "Compiti e scadenze:\n- esercizi a pagina 12"}
    wanted = re.search(r"strumento (\w+)", content) if last["role"] == "user" else None
    names = [t["function"]["name"] for t in tools]
    if wanted and any(n.endswith(wanted.group(1)) for n in names):
        # "usa lo strumento somma con a=2 b=3": chiama lo strumento che finisce con quel nome (test degli strumenti)
        name = next(n for n in names if n.endswith(wanted.group(1)))
        args = {k: (int(v) if v.isdigit() else v) for k, v in re.findall(r"(\w+)=(\w+)", content)}
        msg["tool_calls"] = [{"id": "call_1", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}]
    elif last["role"] == "tool" and any("strumento " in (m.get("content") or "") for m in msgs if m["role"] == "user"):
        msg["content"] = "Risultato: " + str(last.get("content", ""))[:1500]
    elif last["role"] == "user" and "luminosit" in content.lower() and tools:
        n = re.search(r"\d+", content)
        val = int(n.group()) if n else 50
        if "cinquanta" in content.lower():
            val = 50
        name = next(t["function"]["name"] for t in tools if "brightness" in t["function"]["name"])
        msg["tool_calls"] = [{"id": "call_1", "type": "function",
                              "function": {"name": name, "arguments": json.dumps({"brightness": val})}}]
    elif last["role"] == "tool":
        msg["content"] = "Fatto! Ho impostato la luminosità. Posso aiutarti con altro?"
    else:
        msg["content"] = "Ciao! Sono Husky, il tuo assistente locale."
    return msg


async def chat(req):
    body = await req.json()
    msg = answer(body)
    base = {"id": "x", "created": int(time.time()), "model": "mock"}
    if not body.get("stream"):
        return web.json_response({**base, "object": "chat.completion", "choices": [
            {"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(req)

    async def send(delta, finish=None):
        chunk = {**base, "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

    if msg.get("tool_calls"):
        c = msg["tool_calls"][0]
        await send({"role": "assistant", "tool_calls": [{"index": 0, **c}]})
        await send({}, "tool_calls")
    else:
        for word in re.findall(r"\S+\s*", msg["content"]):  # una parola alla volta, come un modello vero
            await send({"content": word})
        await send({}, "stop")
    await resp.write(b"data: [DONE]\n\n")
    return resp


# ---- finto Ollama, per i test della gestione dei modelli (pagina Admin → Modelli)
MODELS = {"qwen3:4b-instruct-2507-q4_K_M": 2497280480}
SEEN = {"model": "", "system": ""}


async def tags(req):
    return web.json_response({"models": [{"name": n, "size": s, "modified_at": "2026-10-01T10:00:00Z",
                                          "details": {"family": "qwen3", "parameter_size": "4B"}} for n, s in MODELS.items()]})


async def pull(req):
    body = await req.json()
    name = body.get("model", "")
    resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
    await resp.prepare(req)
    if name.startswith("inesistente"):
        await resp.write(json.dumps({"error": "pull model manifest: file does not exist"}).encode() + b"\n")
        return resp
    for done in (0, 50, 100):
        await resp.write(json.dumps({"status": "pulling abc", "total": 100, "completed": done}).encode() + b"\n")
    MODELS[name] = 1_000_000_000
    await resp.write(json.dumps({"status": "success"}).encode() + b"\n")
    return resp


async def delete(req):
    body = await req.json()
    if MODELS.pop(body.get("model", ""), None) is None:
        return web.json_response({"error": "model not found"}, status=404)
    return web.json_response({})


async def generate(req):
    return web.json_response({"done": True})


async def seen(req):
    return web.json_response(SEEN)


_chat = chat


async def chat(req):  # noqa: F811 - ricorda modello e istruzioni dell'ultima richiesta (per i test)
    body = await req.json()
    SEEN["model"] = body.get("model", "")
    if body["messages"] and body["messages"][0].get("role") == "system":
        SEEN["system"] = body["messages"][0].get("content", "")
    req._read_bytes = json.dumps(body).encode()
    return await _chat(req)


app = web.Application()
app.router.add_post("/v1/chat/completions", chat)
app.router.add_get("/api/tags", tags)
app.router.add_post("/api/pull", pull)
app.router.add_delete("/api/delete", delete)
app.router.add_post("/api/generate", generate)
app.router.add_get("/mock/seen", seen)
import os  # noqa: E402
web.run_app(app, host=os.environ.get("MOCK_LLM_HOST", "127.0.0.1"), port=1299, print=None)
