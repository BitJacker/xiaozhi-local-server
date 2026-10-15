"""Confronta 1 dispositivo da solo contro N dispositivi in parallelo."""
import asyncio
import sys
import time

import aiohttp

from test_suite import Device, ask_voice

QS = ["Quanto fa due più due?", "Di che colore è il cielo?", "Quante zampe ha un gatto?", "Come si chiama la capitale d'Italia?"]


async def run(n):
    async with aiohttp.ClientSession() as http:
        devs = [await Device(http, device_id=f"aa:bb:cc:00:02:{n}{i}").connect() for i in range(n)]
        t0 = time.time()
        rs = await asyncio.gather(*(ask_voice(d, QS[i % len(QS)], timeout=120) for i, d in enumerate(devs)))
        dt = time.time() - t0
        for d in devs:
            await d.close()
        ok = all(r["stop"] and r["answer"] for r in rs)
        print(f"{n} dispositivi: {dt:.1f}s  ok={ok}")


async def main():
    await run(1)
    await run(3)
    await run(4)

asyncio.run(main())
