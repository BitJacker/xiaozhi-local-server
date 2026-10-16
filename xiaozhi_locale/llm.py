"""Dialogo con un LLM locale (API compatibile OpenAI: LM Studio, Ollama, llama.cpp...)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from openai import AsyncOpenAI

from .tools import ToolSet

log = logging.getLogger("llm")

EMOTIONS = {
    "neutral": "😶", "happy": "🙂", "laughing": "😆", "sad": "😔", "surprised": "😲",
    "thinking": "🤔", "confused": "🙄", "cool": "😎",
}


class LLM:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg.get("api_key") or "locale")

    async def ensure_model(self):
        """Con Ollama scarica il modello se manca (solo la prima volta). Con altri server non fa nulla."""
        import aiohttp

        root = self.cfg["base_url"].rstrip("/").removesuffix("/v1")
        model = self.cfg["model"]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
                async with http.get(f"{root}/api/tags") as r:
                    if r.status != 200:
                        return
                    names = {m.get("name") for m in (await r.json()).get("models", [])}
        except Exception as e:
            log.warning("Server LLM non raggiungibile su %s (%s)", root, e)
            return
        if model in names or f"{model}:latest" in names:
            log.info("Modello LLM '%s' presente (Ollama)", model)
            await self._preload(root, model)
            return
        if not self.cfg.get("auto_pull", True):
            log.warning("Modello '%s' assente in Ollama: esegui  ollama pull %s", model, model)
            return
        log.info("Scarico il modello '%s' con Ollama (solo la prima volta, qualche minuto)...", model)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as http:
            async with http.post(f"{root}/api/pull", json={"model": model, "stream": True}) as r:
                last = ""
                async for line in r.content:
                    try:
                        st = json.loads(line).get("status", "")
                    except json.JSONDecodeError:
                        continue
                    if st != last and not st.startswith("pulling "):
                        log.info("Ollama: %s", st)
                    last = st
        log.info("Modello '%s' pronto", model)
        await self._preload(root, model)

    async def _preload(self, root: str, model: str):
        """Carica subito il modello in memoria: altrimenti la prima domanda aspetta il caricamento."""
        import aiohttp

        t0 = time.time()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as http:
                async with http.post(f"{root}/api/generate", json={"model": model, "keep_alive": -1}) as r:
                    await r.read()
            log.info("Modello LLM caricato in memoria (%.0f s): pronto a rispondere", time.time() - t0)
        except Exception as e:
            log.warning("Precaricamento del modello non riuscito: %s", e)

    async def _create(self, **kwargs):
        """Chiamata con un secondo tentativo: LM Studio a volte sta ancora caricando il modello."""
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            log.warning("LLM: primo tentativo fallito (%s), riprovo", e)
            await asyncio.sleep(3)
            return await self.client.chat.completions.create(**kwargs)

    async def chat(self, messages: list[dict], tools: ToolSet, max_rounds: int = 5) -> str:
        """Esegue il ciclo LLM -> tool -> LLM finché non c'è una risposta testuale.

        `messages` viene aggiornato con i passaggi intermedi (memoria della conversazione).
        """
        specs = tools.specs()
        for _ in range(max_rounds):
            kwargs = dict(
                model=self.cfg["model"], messages=messages,
                temperature=self.cfg.get("temperature", 0.6),
                max_tokens=self.cfg.get("max_tokens", 400),
            )
            if specs:
                kwargs["tools"] = specs
            resp = await self._create(**kwargs)
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls:
                text = (msg.content or "").strip()
                messages.append({"role": "assistant", "content": text})
                return text
            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            })
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("Tool: %s %s", c.function.name, args)
                result = await tools.call(c.function.name, args)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result[:4000]})
        return "Scusa, non sono riuscito a completare la richiesta."

    async def chat_stream(self, messages: list[dict], tools: ToolSet, max_rounds: int = 5):
        """Come chat(), ma restituisce il testo man mano che il modello lo scrive (streaming).

        Così la prima frase può essere letta ad alta voce mentre il modello scrive ancora le altre.
        Se il modello chiama uno strumento, lo esegue e continua. Alla fine (anche se interrotto)
        la risposta viene aggiunta a `messages`.
        """
        if not self.cfg.get("stream", True):
            yield await self.chat(messages, tools, max_rounds)
            return
        specs = tools.specs()
        for _ in range(max_rounds):
            kwargs = dict(
                model=self.cfg["model"], messages=messages, stream=True,
                temperature=self.cfg.get("temperature", 0.6),
                max_tokens=self.cfg.get("max_tokens", 400),
            )
            if specs:
                kwargs["tools"] = specs
            stream = await self._create(**kwargs)
            text, calls = [], {}
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    for tc in getattr(delta, "tool_calls", None) or []:
                        c = calls.setdefault(tc.index or 0, {"id": "", "name": "", "args": ""})
                        c["id"] = tc.id or c["id"]
                        if tc.function is not None:
                            c["name"] += tc.function.name or ""
                            c["args"] += tc.function.arguments or ""
                    if delta.content:
                        text.append(delta.content)
                        if not calls:  # il testo che precede una chiamata a strumento non si legge
                            yield delta.content
            finally:
                if not calls:
                    messages.append({"role": "assistant", "content": "".join(text).strip()})
            if not calls:
                return
            ordered = [calls[i] for i in sorted(calls)]
            for n, c in enumerate(ordered):
                c["id"] = c["id"] or f"call_{n}"
            messages.append({
                "role": "assistant", "content": "".join(text),
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for c in ordered],
            })
            for c in ordered:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("Tool: %s %s", c["name"], args)
                result = await tools.call(c["name"], args if isinstance(args, dict) else {})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result[:4000]})
        yield "Scusa, non sono riuscito a completare la richiesta."

    async def describe_image(self, jpeg: bytes, question: str) -> str:
        model = self.cfg.get("vision_model")
        if not model:
            return "La visione non è configurata sul server locale."
        b64 = base64.b64encode(jpeg).decode()
        resp = await self.client.chat.completions.create(
            model=model, max_tokens=300,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": (question or "Descrivi l'immagine.") + " Rispondi in italiano, in breve."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
        )
        return (resp.choices[0].message.content or "").strip()
