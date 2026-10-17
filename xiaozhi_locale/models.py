"""Gestione dei modelli dalla pagina Admin: modello linguistico (Ollama), Whisper e voci Piper.

* Ollama: elenco dei modelli installati, scaricamento con avanzamento, eliminazione e scelta di quello attivo,
  senza riavviare il server (la domanda successiva usa già il modello nuovo).
* Whisper: scelta del modello (tiny ... large-v3-turbo); quello nuovo si carica in sottofondo e prende il posto del
  vecchio solo quando è pronto.
* Voci Piper: scaricamento da un elenco fisso (niente indirizzi arbitrari), scelta della voce predefinita,
  eliminazione delle voci non usate.

Scaricare un modello è l'unico momento in cui il server contatta Internet (registro di Ollama o Hugging Face), e
solo quando l'admin preme il pulsante. Le conversazioni restano sempre sul server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from .settings import MODEL_RE, WHISPER_MODELS

log = logging.getLogger("models")

PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{region}/{name}/{quality}/{voice}"
# voci Piper che si possono scaricare dalla pagina Admin (licenze nel MODEL_CARD di ogni voce)
PIPER_CATALOG = [
    "it_IT-paola-medium", "it_IT-riccardo-x_low",
    "en_US-lessac-medium", "en_US-amy-medium", "en_GB-alba-medium",
    "fr_FR-siwis-medium", "de_DE-thorsten-medium", "es_ES-davefx-medium",
]
# modelli consigliati per Husky (sanno usare gli strumenti); se ne può scrivere anche un altro
LLM_SUGGESTED = [
    {"name": "qwen3:4b-instruct-2507-q4_K_M", "size": "2,5 GB", "note": "predefinito, GPU da 6 GB"},
    {"name": "qwen3:8b", "size": "5,2 GB", "note": "più preciso, GPU da 8 GB"},
    {"name": "llama3.2:3b", "size": "2,0 GB", "note": "più leggero"},
    {"name": "llama3.1:8b", "size": "4,9 GB", "note": "GPU da 8 GB"},
    {"name": "mistral-nemo", "size": "7,1 GB", "note": "GPU da 12 GB"},
]
WHISPER_INFO = {"tiny": "75 MB, molto veloce, poco preciso", "base": "145 MB", "small": "480 MB, buono anche su CPU",
                "medium": "1,5 GB", "large-v3-turbo": "1,6 GB, il migliore su GPU", "large-v3": "3 GB, lento"}


class ModelManager:
    def __init__(self, app):
        self.app = app
        self.jobs: dict[str, dict] = {}  # scaricamenti e caricamenti in corso (per la pagina Admin)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ Ollama
    @property
    def ollama_root(self) -> str:
        return self.app["cfg"]["llm"]["base_url"].rstrip("/").removesuffix("/v1")

    async def _ollama(self, method: str, path: str, payload: dict | None = None, timeout: float = 10):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            async with http.request(method, self.ollama_root + path, json=payload) as r:
                body = await r.text()
                if r.status >= 400:
                    raise RuntimeError(_ollama_error(body) or f"errore {r.status}")
                return json.loads(body) if body.strip().startswith("{") else {}

    async def llm_models(self) -> dict:
        try:
            tags = await self._ollama("GET", "/api/tags")
        except Exception as e:
            return {"ollama": False, "error": f"Ollama non risponde ({e})", "installed": []}
        installed = [{"name": m.get("name", ""), "size": m.get("size", 0),
                      "modified": (m.get("modified_at") or "")[:10],
                      "family": (m.get("details") or {}).get("family", ""),
                      "params": (m.get("details") or {}).get("parameter_size", "")}
                     for m in tags.get("models", [])]
        return {"ollama": True, "installed": sorted(installed, key=lambda m: m["name"])}

    def start_pull(self, name: str) -> dict:
        name = name.strip()
        if not MODEL_RE.match(name):
            raise ValueError("Nome del modello non valido")
        key = f"llm:{name}"
        if self.jobs.get(key, {}).get("state") == "running":
            raise ValueError("Lo sto già scaricando")
        job = self.jobs[key] = {"kind": "llm", "name": name, "state": "running", "status": "inizio", "percent": 0,
                                "started": time.time()}
        self._spawn(self._pull(name, job))
        return job

    async def _pull(self, name: str, job: dict):
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600)) as http:
                async with http.post(self.ollama_root + "/api/pull", json={"model": name, "stream": True}) as r:
                    if r.status >= 400:
                        raise RuntimeError(_ollama_error(await r.text()) or f"errore {r.status}")
                    async for line in r.content:
                        try:
                            st = json.loads(line)
                        except ValueError:
                            continue
                        if st.get("error"):
                            raise RuntimeError(st["error"])
                        job["status"] = st.get("status", job["status"])
                        if st.get("total"):
                            job["percent"] = round(100 * st.get("completed", 0) / st["total"])
            job.update(state="done", percent=100, status="pronto")
            log.info("Modello '%s' scaricato con Ollama", name)
        except Exception as e:
            job.update(state="error", status=str(e)[:200])
            log.warning("Scaricamento del modello '%s' non riuscito: %s", name, e)

    async def delete_llm(self, name: str):
        if not MODEL_RE.match(name):
            raise ValueError("Nome del modello non valido")
        if name == self.app["cfg"]["llm"]["model"]:
            raise ValueError("È il modello in uso: prima scegline un altro")
        await self._ollama("DELETE", "/api/delete", {"model": name})

    async def use_llm(self, name: str):
        """Il modello nuovo vale dalla domanda successiva; intanto lo si carica in memoria."""
        names = {m["name"] for m in (await self.llm_models())["installed"]}
        if name not in names and f"{name}:latest" not in names:
            raise ValueError("Modello non installato: prima scaricalo")
        self.app["settings"].set("llm.model", name)
        self._spawn(self.app["llm"]._preload(self.ollama_root, name))

    # ------------------------------------------------------------------ Whisper
    def whisper_status(self) -> dict:
        asr = self.app["asr"]
        return {"current": getattr(asr, "model_name", self.app["cfg"].get("asr", {}).get("model", "")),
                "device": getattr(asr, "device", ""), "choices": [{"name": n, "note": WHISPER_INFO[n]} for n in WHISPER_MODELS]}

    def start_whisper(self, name: str) -> dict:
        if name not in WHISPER_MODELS:
            raise ValueError("Modello di Whisper non valido")
        if any(j["kind"] == "asr" and j["state"] == "running" for j in self.jobs.values()):
            raise ValueError("Sto già caricando un modello di Whisper")
        job = self.jobs["asr"] = {"kind": "asr", "name": name, "state": "running",
                                  "status": "scarico e carico il modello", "percent": 0, "started": time.time()}
        self._spawn(self._whisper(name, job))
        return job

    async def _whisper(self, name: str, job: dict):
        try:
            await self.app["asr"].reload(name)
            self.app["settings"].set("asr.model", name)
            job.update(state="done", percent=100, status=f"in uso ({self.app['asr'].device})")
        except Exception as e:
            job.update(state="error", status=str(e)[:200])
            log.warning("Cambio del modello di Whisper non riuscito: %s", e)

    # ------------------------------------------------------------------ voci Piper
    @property
    def voice_dir(self) -> Path:
        return Path(os.path.dirname(self.app["cfg"]["tts"]["voice"]) or ".")

    def voices(self) -> dict:
        installed = self.app["voices"].available()
        used = {Path(self.app["cfg"]["tts"]["voice"]).name} | {
            p.get("voice") for p in self.app["personas"].items() if p.get("voice")}
        return {"installed": [{"name": v, "default": v == Path(self.app["cfg"]["tts"]["voice"]).name,
                               "used": v in used, "size": _size(self.voice_dir / v)} for v in installed],
                "catalog": [v for v in PIPER_CATALOG if f"{v}.onnx" not in installed]}

    def start_voice(self, voice: str) -> dict:
        if voice not in PIPER_CATALOG:
            raise ValueError("Voce non presente nell'elenco")
        key = f"voice:{voice}"
        if self.jobs.get(key, {}).get("state") == "running":
            raise ValueError("La sto già scaricando")
        job = self.jobs[key] = {"kind": "voice", "name": voice, "state": "running", "status": "scarico",
                                "percent": 0, "started": time.time()}
        self._spawn(self._download_voice(voice, job))
        return job

    async def _download_voice(self, voice: str, job: dict):
        import aiohttp
        lang_region, name, quality = voice.split("-")
        lang = lang_region.split("_")[0]
        try:
            self.voice_dir.mkdir(parents=True, exist_ok=True)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as http:
                for ext in (".onnx.json", ".onnx"):
                    url = PIPER_URL.format(lang=lang, region=lang_region, name=name, quality=quality, voice=voice + ext)
                    tmp = self.voice_dir / f"{voice}{ext}.part"
                    async with http.get(url) as r:
                        if r.status != 200:
                            raise RuntimeError(f"Hugging Face ha risposto {r.status}")
                        total, done = int(r.headers.get("Content-Length") or 0), 0
                        with open(tmp, "wb") as f:
                            async for chunk in r.content.iter_chunked(1 << 16):
                                f.write(chunk)
                                done += len(chunk)
                                if ext == ".onnx" and total:
                                    job["percent"] = round(100 * done / total)
                    os.replace(tmp, self.voice_dir / f"{voice}{ext}")
            job.update(state="done", percent=100, status="pronta")
            log.info("Voce Piper '%s' scaricata", voice)
        except Exception as e:
            for ext in (".onnx.json.part", ".onnx.part"):
                (self.voice_dir / f"{voice}{ext}").unlink(missing_ok=True)
            job.update(state="error", status=str(e)[:200])

    async def use_voice(self, filename: str):
        if filename not in self.app["voices"].available():
            raise ValueError("Voce non installata")
        path = str(self.voice_dir / filename)
        await asyncio.get_running_loop().run_in_executor(None, self.app["tts"].load_voice, path)
        self.app["settings"].set("tts.voice", filename)
        self.app["voices"].clear()

    def delete_voice(self, filename: str):
        info = next((v for v in self.voices()["installed"] if v["name"] == filename), None)
        if info is None:
            raise ValueError("Voce non installata")
        if info["used"]:
            raise ValueError("Voce in uso (predefinita o di un personaggio)")
        for ext in ("", ".json"):
            (self.voice_dir / f"{filename}{ext}").unlink(missing_ok=True)
        self.app["voices"].clear()

    # ------------------------------------------------------------------
    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def public_jobs(self) -> list[dict]:
        cutoff = time.time() - 3600  # i lavori finiti da più di un'ora non si mostrano più
        return [j for j in self.jobs.values() if j["state"] == "running" or j["started"] > cutoff]


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _ollama_error(body: str) -> str:
    try:
        return str(json.loads(body).get("error", ""))[:200]
    except ValueError:
        return re.sub(r"\s+", " ", body)[:200]
