"""Test di affidabilità prolungato: uso continuo + telecamera che si spegne + cadute di rete.

Avvia un server di prova (porta 8090, HuskyLens simulata) e per N minuti:
  * ogni ~15 s un dispositivo fa una domanda (una su quattro a voce, le altre scritte);
  * ogni ~90 s la HuskyLens simulata si spegne per 20 s e si riaccende: il server deve ricollegarsi da solo
    e tornare a rispondere a "cosa vedi?";
  * ogni ~60 s una connessione viene chiusa di colpo a metà risposta (Wi-Fi che cade);
  * misura la memoria del server per scoprire eventuali perdite.
Serve Ollama acceso.  Uso:  python tests/soak_test.py [minuti]
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from run_all import make_config  # noqa: E402
from test_suite import speech  # noqa: E402

from xiaozhi_locale.audio import OpusEncoder  # noqa: E402

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 10
PORT = 8090
BASE = f"http://localhost:{PORT}"
PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
QUESTIONS = ["Quanto fa sei per sette?", "Dimmi il nome di un pianeta.", "Che giorno è oggi?",
             "Dimmi un sinonimo di veloce.", "Quante zampe ha un ragno?", "Qual è la capitale della Spagna?"]

stats = {"ok": 0, "fail": 0, "lat": [], "vision_ok": 0, "vision_fail": 0, "drops": 0, "reconnect_s": [],
         "errors": []}


def health() -> dict:
    try:
        return json.loads(urllib.request.urlopen(BASE + "/health", timeout=3).read())
    except Exception:
        return {}


async def ask(http, text=None, voice=None, drop=False, timeout=90) -> str | None:
    headers = {"Device-Id": "aa:bb:cc:dd:ee:99", "Client-Id": "soak", "Authorization": "Bearer test123"}
    async with http.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/", headers=headers) as ws:
        await ws.send_json({"type": "hello", "transport": "websocket",
                            "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}})
        await ws.receive(timeout=10)
        if voice:
            await ws.send_json({"type": "listen", "state": "start", "mode": "manual"})
            for p in OpusEncoder(16000, 60).encode(speech(voice, 0.5)):
                await ws.send_bytes(p)
                await asyncio.sleep(0.06)
            await ws.send_json({"type": "listen", "state": "stop"})
        else:
            await ws.send_json({"type": "text", "text": text})
        parts, t0 = [], time.time()
        while time.time() - t0 < timeout:
            m = await ws.receive(timeout=timeout)
            if m.type == aiohttp.WSMsgType.BINARY:
                if drop:
                    return "drop"  # chiude di colpo appena arriva l'audio (Wi-Fi che cade)
                continue
            if m.type != aiohttp.WSMsgType.TEXT:
                return None
            d = json.loads(m.data)
            if d["type"] == "tts" and d.get("state") == "sentence_start":
                parts.append(d["text"])
            if d["type"] == "tts" and d.get("state") == "stop":
                return " ".join(parts)
    return None


async def main():
    data = ROOT / "tests" / "_data_soak"
    shutil.rmtree(data, ignore_errors=True)
    cfg_path = ROOT / "tests" / "config.soak.yaml"
    cfg_path.write_text(make_config(PORT, "tests/_data_soak", auth=False, turns=1000, huskylens=True), encoding="utf-8")
    husky = subprocess.Popen([PY, "tests/fake_huskylens.py"], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = subprocess.Popen([PY, "server.py", "-c", str(cfg_path)], cwd=ROOT, env=ENV,
                              stdout=open(data.parent / "_soak_server.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if health().get("mcp_esterni", {}).get("huskylens"):
                break
            await asyncio.sleep(2)
        proc = psutil.Process(server.pid)
        real = [c for c in proc.children(recursive=True) if "python" in c.name().lower()] or [proc]
        mem = lambda: sum(p.memory_info().rss for p in [proc, *real]) / 2 ** 20  # noqa: E731
        await asyncio.sleep(20)  # modelli caricati e precaricati
        mem_start = mem()
        mem_samples = [mem_start]
        end = time.time() + MINUTES * 60
        t_next_cam = time.time() + 90
        t_next_drop = time.time() + 60
        i = 0
        async with aiohttp.ClientSession() as http:
            while time.time() < end:
                i += 1
                # ------------------------------ telecamera spenta e riaccesa
                if time.time() >= t_next_cam:
                    t_next_cam = time.time() + 90
                    husky.terminate()
                    husky.wait()
                    print(f"[{i}] HuskyLens spenta", flush=True)
                    await asyncio.sleep(20)
                    husky = subprocess.Popen([PY, "tests/fake_huskylens.py"], cwd=ROOT, env=ENV,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    t0 = time.time()
                    while not health().get("mcp_esterni", {}).get("huskylens") and time.time() - t0 < 60:
                        await asyncio.sleep(1)
                    stats["reconnect_s"].append(round(time.time() - t0, 1))
                    ans = await ask(http, "Cosa vedi con la telecamera?")
                    ok = bool(ans) and "alex" in ans.lower()
                    stats["vision_ok" if ok else "vision_fail"] += 1
                    print(f"[{i}] HuskyLens riaccesa: ricollegata in {stats['reconnect_s'][-1]} s, risposta: {ans!r:.70}", flush=True)
                # ------------------------------ Wi-Fi che cade a metà risposta
                if time.time() >= t_next_drop:
                    t_next_drop = time.time() + 60
                    try:
                        await ask(http, "Raccontami una storia lunga su un gatto.", drop=True)
                        stats["drops"] += 1
                    except Exception as e:
                        stats["errors"].append(f"drop: {e}")
                # ------------------------------ domanda normale
                q = QUESTIONS[i % len(QUESTIONS)]
                t0 = time.time()
                try:
                    ans = await ask(http, voice=q) if i % 4 == 0 else await ask(http, q)
                except Exception as e:
                    ans = None
                    stats["errors"].append(f"{q}: {e!r}"[:120])
                if ans and "non risponde" not in ans:
                    stats["ok"] += 1
                    stats["lat"].append(time.time() - t0)
                else:
                    stats["fail"] += 1
                if server.poll() is not None:
                    stats["errors"].append(f"SERVER TERMINATO (exit {server.returncode})")
                    break
                mem_samples.append(mem())
                left = int(end - time.time())
                print(f"[{i}] {'OK' if ans else 'KO'} {time.time() - t0:4.1f}s  mem {mem_samples[-1]:.0f} MB  "
                      f"(mancano {left // 60}:{left % 60:02d})  {q}", flush=True)
                await asyncio.sleep(10)
    finally:
        alive = server.poll() is None
        server.terminate()
        husky.terminate()

    lat = stats["lat"]
    total = stats["ok"] + stats["fail"]
    growth = mem_samples[-1] - mem_samples[1] if len(mem_samples) > 2 else 0
    print("\n================ RISULTATO TEST DI AFFIDABILITÀ ================")
    print(f"Durata: {MINUTES:.0f} minuti   domande: {total}   riuscite: {stats['ok']}   fallite: {stats['fail']}")
    if lat:
        print(f"Tempo di risposta: medio {statistics.mean(lat):.1f} s, massimo {max(lat):.1f} s")
    print(f"Telecamera spenta/riaccesa: {len(stats['reconnect_s'])} volte, ricollegata in {stats['reconnect_s']} s, "
          f"'cosa vedi' corretto {stats['vision_ok']}/{stats['vision_ok'] + stats['vision_fail']}")
    print(f"Cadute di rete a metà risposta: {stats['drops']} (il server deve restare in piedi)")
    print(f"Memoria del server: {mem_samples[1]:.0f} MB -> {mem_samples[-1]:.0f} MB (variazione {growth:+.0f} MB)")
    print(f"Server ancora acceso alla fine: {'sì' if alive else 'NO'}")
    for e in stats["errors"][:10]:
        print("  errore:", e)
    ok = alive and total and stats["fail"] <= max(1, total // 20) and stats["vision_fail"] == 0 and growth < 300
    print("ESITO:", "OK" if ok else "FALLITO")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
