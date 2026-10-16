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
    wanted = re.search(r"strumento (\w+)", content) if last["role"] == "user" else None
    names = [t["function"]["name"] for t in tools]
    if wanted and any(n.endswith(wanted.group(1)) for n in names):
        # "usa lo strumento somma con a=2 b=3": chiama lo strumento che finisce con quel nome (test degli strumenti)
        name = next(n for n in names if n.endswith(wanted.group(1)))
        args = {k: (int(v) if v.isdigit() else v) for k, v in re.findall(r"(\w+)=(\w+)", content)}
        msg["tool_calls"] = [{"id": "call_1", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}]
    elif last["role"] == "tool" and any("strumento " in (m.get("content") or "") for m in msgs if m["role"] == "user"):
        msg["content"] = "Risultato: " + str(last.get("content", ""))[:300]
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


app = web.Application()
app.router.add_post("/v1/chat/completions", chat)
web.run_app(app, host="127.0.0.1", port=1299, print=None)
