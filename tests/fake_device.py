"""Test end-to-end: simula un dispositivo xiaozhi (ESP32 / HuskyLens) contro il server locale.

1. chiede l'OTA e riceve l'URL WebSocket
2. hello con MCP; espone un tool finto "self.screen.set_brightness"
3. invia una frase parlata (generata con Piper) codificata in Opus 16 kHz / 60 ms
4. verifica stt, chiamata al tool, tts e audio Opus di risposta

Uso:  .venv\\Scripts\\python tests\\fake_device.py [http://localhost:8000] ["frase"]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xiaozhi_locale.audio import OpusDecoder, OpusEncoder, resample  # noqa: E402
from xiaozhi_locale.engines import TTS  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PHRASE = sys.argv[2] if len(sys.argv) > 2 else "Imposta la luminosità dello schermo a cinquanta."


async def main():
    tts = TTS({"voice": str(Path(__file__).resolve().parent.parent / "models/piper/it_IT-paola-medium.onnx")})
    speech = resample(tts._run(PHRASE), tts.sample_rate, 16000)
    speech = np.concatenate([np.zeros(4800, np.int16), speech, np.zeros(16000 * 2, np.int16)])  # + 2 s silenzio
    packets = OpusEncoder(16000, 60).encode(speech)

    headers = {"Device-Id": "aa:bb:cc:dd:ee:ff", "Client-Id": "test-client", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{BASE}/xiaozhi/ota/", json={"application": {"version": "1.6.0"}}, headers=headers) as r:
            ota = await r.json()
        print("OTA ->", json.dumps(ota, ensure_ascii=False))
        ws_url, token = ota["websocket"]["url"], ota["websocket"]["token"]
        ws_url = ws_url.replace(ws_url.split("//")[1].split("/")[0], BASE.split("//")[1])  # usa l'host di test

        result = {"stt": None, "tool_called": None, "sentences": [], "audio_packets": 0, "tts_stop": False}
        dec = OpusDecoder(24000)
        decoded = 0
        async with http.ws_connect(ws_url, headers={**headers, "Authorization": f"Bearer {token}", "Protocol-Version": "1"}) as ws:
            await ws.send_json({"type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
                                "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
            hello = await ws.receive_json(timeout=10)
            assert hello["type"] == "hello" and hello["transport"] == "websocket", hello
            sid = hello["session_id"]
            print("hello <-", hello)

            async def sender():
                await asyncio.sleep(1.0)  # lascia tempo allo scambio MCP
                await ws.send_json({"session_id": sid, "type": "listen", "state": "start", "mode": "auto"})
                for p in packets:
                    await ws.send_bytes(p)
                    await asyncio.sleep(0.06)
            send_task = asyncio.create_task(sender())

            t_end_audio = None
            deadline = time.time() + 120
            while time.time() < deadline and not result["tts_stop"]:
                msg = await ws.receive(timeout=120)
                if msg.type == aiohttp.WSMsgType.BINARY:
                    result["audio_packets"] += 1
                    decoded += len(dec.decode(msg.data))
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                m = json.loads(msg.data)
                if m["type"] == "mcp":
                    p = m["payload"]
                    if p.get("method") == "initialize":
                        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
                    elif p.get("method") == "tools/list":
                        res = {"tools": [{"name": "self.screen.set_brightness", "description": "Imposta la luminosità dello schermo (0-100)",
                                          "inputSchema": {"type": "object", "properties": {"brightness": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["brightness"]}}],
                               "nextCursor": ""}
                    elif p.get("method") == "tools/call":
                        result["tool_called"] = p["params"]
                        res = {"content": [{"type": "text", "text": "true"}], "isError": False}
                    else:
                        continue
                    await ws.send_json({"session_id": sid, "type": "mcp", "payload": {"jsonrpc": "2.0", "id": p["id"], "result": res}})
                elif m["type"] == "stt":
                    result["stt"] = m["text"]
                    t_end_audio = time.time()
                    print("stt <-", m["text"])
                elif m["type"] == "tts":
                    if m["state"] == "sentence_start":
                        result["sentences"].append(m["text"])
                        print("tts <-", m["text"])
                    elif m["state"] == "stop":
                        result["tts_stop"] = True
                elif m["type"] == "llm":
                    print("llm <-", m.get("emotion"))
            send_task.cancel()

        result["audio_seconds"] = round(decoded / 16000, 2)
        if t_end_audio:
            result["secondi_dalla_stt_a_fine_risposta"] = round(time.time() - t_end_audio, 1)
        print("\nRISULTATO:", json.dumps(result, ensure_ascii=False, indent=2))
        ok = result["stt"] and result["sentences"] and result["audio_packets"] > 0 and result["tts_stop"]
        print("ESITO:", "OK" if ok else "FALLITO")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
