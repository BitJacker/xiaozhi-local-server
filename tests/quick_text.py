"""Prova veloce via testo: manda domande al server e stampa le risposte (nessuna voce)."""
import asyncio
import json
import sys

import aiohttp

URL = "ws://localhost:8000/xiaozhi/v1/?device-id=quicktest"
QUESTIONS = sys.argv[1:] or ["Ciao, cosa puoi fare?", "Cosa vedi con la telecamera?", "Deo."]


async def main():
    async with aiohttp.ClientSession() as http, http.ws_connect(URL) as ws:
        await ws.send_json({"type": "hello", "transport": "websocket",
                            "audio_params": {"format": "pcm", "sample_rate": 16000, "channels": 1}})
        await ws.receive(timeout=10)
        for q in QUESTIONS:
            await ws.send_json({"type": "text", "text": q})
            parts = []
            while True:
                m = await ws.receive(timeout=90)
                if m.type != aiohttp.WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d["type"] == "tts" and d.get("state") == "sentence_start":
                    parts.append(d["text"])
                if d["type"] == "tts" and d.get("state") == "stop":
                    break
            print(f"D: {q}\nR: {' '.join(parts)}\n")

asyncio.run(main())
