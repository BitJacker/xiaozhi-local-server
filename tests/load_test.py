"""Test di carico: quanti studenti possono parlare con Husky nello stesso momento?

Avvia un server di prova (o usa uno già acceso con --url) e fa parlare N "studenti" insieme: ognuno manda una domanda a
voce (voce sintetica, al ritmo reale come un microfono) e misura quanto aspetta il primo suono della risposta e la fine
della risposta. Ripete con gruppi sempre più grandi e stampa una tabella; i numeri finiscono anche in
tests/_load_result.json. Con --mock il modello linguistico è finto: misura solo il server (voce, riconoscimento, sintesi).

Uso:  python tests/load_test.py [--mock] [--levels 1,5,10,20] [--url http://127.0.0.1:8000 --token XXX]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from run_all import MOCK, PY, env, make_config, stop, wait_health  # noqa: E402

QUESTIONS = ["Quanto fa sette più otto?", "Quanto fa nove per tre?", "Quanto fa dodici meno cinque?",
             "Quanto fa sei per sei?", "Quanto fa venti diviso quattro?", "Quanto fa tre più quattro?"]
PORT = 8103


def voices() -> list[np.ndarray]:
    from xiaozhi_locale.audio import resample
    from xiaozhi_locale.engines import TTS
    tts = TTS({"voice": str(ROOT / "models/piper/it_IT-paola-medium.onnx")})
    return [resample(tts._run(q), tts.sample_rate, 16000) for q in QUESTIONS]


async def student(url: str, token: str, n: int, pcm: np.ndarray, timeout: float) -> dict:
    ws_url = url.replace("http", "ws") + "/xiaozhi/v1/"
    res = {"ok": False, "first_audio": None, "total": None, "stt": None, "error": ""}
    headers = {"Authorization": f"Bearer {token}", "Device-Id": f"carico-{n:03d}", "Client-Id": f"c{n}"}
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(ws_url, headers=headers, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                await ws.send_json({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}})
                await ws.receive_json(timeout=15)
                await ws.send_json({"type": "listen", "state": "start", "mode": "manual"})
                for i in range(0, len(pcm), 960):  # 60 ms alla volta, come un microfono
                    await ws.send_bytes(pcm[i:i + 960].tobytes())
                    await asyncio.sleep(0.06)
                await ws.send_json({"type": "listen", "state": "stop", "mode": "manual"})
                t0 = time.monotonic()
                deadline = t0 + timeout
                while time.monotonic() < deadline:
                    msg = await ws.receive(timeout=max(0.1, deadline - time.monotonic()))
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        if res["first_audio"] is None:
                            res["first_audio"] = time.monotonic() - t0
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        m = json.loads(msg.data)
                        if m.get("type") == "stt":
                            res["stt"] = m.get("text")
                        elif m.get("type") == "tts" and m.get("state") == "stop" and res["first_audio"] is not None:
                            res["total"] = time.monotonic() - t0
                            res["ok"] = True
                            break
                    else:
                        res["error"] = "connessione chiusa"
                        break
                else:
                    res["error"] = "tempo scaduto"
    except Exception as e:
        res["error"] = type(e).__name__ + (f": {e}" if str(e) else "")
    return res


def pct(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))] if vals else None


async def run(url: str, token: str, levels: list[int], timeout: float) -> list[dict]:
    clips = voices()
    rows = []
    for n in levels:
        t0 = time.monotonic()
        res = await asyncio.gather(*(student(url, token, i, clips[i % len(clips)], timeout) for i in range(n)))
        good = [r for r in res if r["ok"]]
        fa = [r["first_audio"] for r in good]
        tot = [r["total"] for r in good]
        row = {"studenti": n, "riusciti": len(good), "primo_audio_mediana": statistics.median(fa) if fa else None,
               "primo_audio_p90": pct(fa, 90), "primo_audio_max": max(fa) if fa else None,
               "fine_risposta_p90": pct(tot, 90), "durata_prova": time.monotonic() - t0,
               "errori": sorted({r["error"] for r in res if r["error"]})}
        rows.append(row)
        f = lambda v: f"{v:5.1f} s" if v is not None else "   —  "  # noqa: E731
        print(f"{n:3d} studenti: riusciti {len(good):3d}/{n:<3d}  primo audio mediana {f(row['primo_audio_mediana'])}"
              f"  p90 {f(row['primo_audio_p90'])}  max {f(row['primo_audio_max'])}  fine risposta p90 "
              f"{f(row['fine_risposta_p90'])}  {', '.join(row['errori'])}", flush=True)
        await asyncio.sleep(3)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--levels", default="1,5,10,20")
    ap.add_argument("--url")
    ap.add_argument("--token", default="carico")
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]
    procs, data = [], ROOT / "tests" / "_data_load"
    url = args.url
    if not url:
        shutil.rmtree(data, ignore_errors=True)
        cfg = make_config(PORT, "tests/_data_load", auth=False, turns=100000, huskylens=False)
        cfg = cfg.replace('tokens: ["test123"]', f'tokens: ["{args.token}"]')
        cfg = cfg.replace("ws_per_ip: 6", f"ws_per_ip: {max(levels) + 5}")
        cfg = cfg.replace("requests_per_minute: 240", "requests_per_minute: 100000")
        (ROOT / "tests" / "config.load.yaml").write_text(cfg, encoding="utf-8")
        if MOCK:
            procs.append(subprocess.Popen([PY, "tests/mock_llm.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL))
        data.mkdir(parents=True)
        log = open(data / "server.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen([PY, "server.py", "-c", "tests/config.load.yaml"], cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT))
        url = f"http://127.0.0.1:{PORT}"
        if not wait_health(url + "/health", '"ok"'):
            print("server non partito")
            stop(procs)
            return 1
    try:
        print(f"Test di carico su {url} ({'LLM finto' if MOCK else 'modello vero'}): una domanda a voce per studente\n")
        rows = asyncio.run(run(url, args.token, levels, args.timeout))
    finally:
        stop(procs)
        (ROOT / "tests" / "config.load.yaml").unlink(missing_ok=True)
    out = {"quando": time.strftime("%Y-%m-%d %H:%M"), "llm": "finto" if MOCK else "vero", "livelli": rows}
    (ROOT / "tests" / "_load_result.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    ok = all(r["riusciti"] == r["studenti"] for r in rows)
    if ok and not args.url:
        shutil.rmtree(data, ignore_errors=True)
    print("\nESITO:", "tutte le domande hanno avuto risposta" if ok else "alcune domande non hanno avuto risposta")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
