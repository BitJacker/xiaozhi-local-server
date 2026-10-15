"""LLM finto compatibile OpenAI, solo per i test (porta 1299)."""
import json, re, time
from aiohttp import web

async def chat(req):
    body = await req.json()
    msgs, tools = body["messages"], body.get("tools") or []
    last = msgs[-1]
    msg = {"role": "assistant", "content": ""}
    if last["role"] == "user" and "luminosit" in last["content"].lower() and tools:
        n = re.search(r"\d+", last["content"]); val = int(n.group()) if n else 50
        if "cinquanta" in last["content"].lower(): val = 50
        name = next(t["function"]["name"] for t in tools if "brightness" in t["function"]["name"])
        msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps({"brightness": val})}}]
    elif last["role"] == "tool":
        msg["content"] = "Fatto! Ho impostato la luminosità. Posso aiutarti con altro?"
    else:
        msg["content"] = "Ciao! Sono Husky, il tuo assistente locale."
    return web.json_response({"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "mock",
                              "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]})

app = web.Application(); app.router.add_post("/v1/chat/completions", chat)
web.run_app(app, host="127.0.0.1", port=1299, print=None)
