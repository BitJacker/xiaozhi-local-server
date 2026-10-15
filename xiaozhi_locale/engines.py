"""Motori locali: riconoscimento vocale (faster-whisper) e sintesi vocale (Piper)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger("engines")
_asr_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr")  # 2 trascrizioni in parallelo
_tts_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tts")


def _add_cuda_dlls():
    """Rende visibili le librerie CUDA installate via pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        for sub in ("cublas", "cudnn"):
            if sys.platform == "win32":
                d = os.path.join(root, sub, "bin")
                if os.path.isdir(d):
                    os.add_dll_directory(d)
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            else:
                # Linux: LD_LIBRARY_PATH è letto solo all'avvio, quindi si precaricano le .so
                import ctypes
                import glob
                d = os.path.join(root, sub, "lib")
                for lib in sorted(glob.glob(os.path.join(d, "lib*.so.*"))):
                    try:
                        ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass


def cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class ASR:
    def __init__(self, cfg: dict):
        _add_cuda_dlls()
        from faster_whisper import WhisperModel

        self.language = cfg.get("language", "it")
        device = cfg.get("device", "auto")
        compute = cfg.get("compute_type", "auto")
        model = cfg.get("model", "small")
        self.prompt = cfg.get("prompt") or None
        self.beam_size = int(cfg.get("beam_size", 5))
        if device == "auto" and not cuda_available():
            device = "cpu"
        if device == "cpu":
            # senza GPU i modelli grandi sono troppo lenti: si usa quello per CPU
            model, compute = cfg.get("cpu_model", "small"), "int8"
        try:
            self.model = WhisperModel(model, device=device, compute_type=compute, num_workers=2)
            # Prova reale: su alcune GPU mancano le librerie CUDA e l'errore emerge solo qui.
            noise = (np.random.default_rng(0).standard_normal(16000) * 0.1).astype(np.float32)
            segs, _ = self.model.transcribe(noise, language=self.language)
            list(segs)  # i segmenti sono pigri: va consumato per eseguire davvero il modello
            log.info("Whisper '%s' caricato (device=%s, %s)", model, self.model.model.device, compute)
        except Exception as e:  # fallback CPU
            log.warning("Whisper su %s non disponibile (%s): uso CPU", device, e)
            cpu_model = cfg.get("cpu_model", "small")
            self.model = WhisperModel(cpu_model, device="cpu", compute_type="int8", num_workers=2)
            log.info("Whisper '%s' caricato su CPU", cpu_model)

    def _run(self, pcm: np.ndarray) -> str:
        audio = pcm.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio, language=self.language, beam_size=self.beam_size, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=self.prompt,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm: np.ndarray) -> str:
        return await asyncio.get_running_loop().run_in_executor(_asr_pool, self._run, pcm)


class TTS:
    def __init__(self, cfg: dict):
        from piper import PiperVoice
        from piper.config import SynthesisConfig

        self.voice = PiperVoice.load(cfg["voice"])
        self.sample_rate = self.voice.config.sample_rate
        self.syn = SynthesisConfig(
            length_scale=float(cfg.get("length_scale", 1.0)),
            noise_scale=cfg.get("noise_scale"),
            noise_w_scale=cfg.get("noise_w_scale"),
        )
        # pausa di silenzio aggiunta alla fine di ogni frase (respiro naturale)
        self.pause = np.zeros(int(self.sample_rate * cfg.get("sentence_pause_ms", 0) / 1000), dtype=np.int16)
        log.info("Voce Piper caricata: %s (%d Hz)", cfg["voice"], self.sample_rate)

    def _run(self, text: str) -> np.ndarray:
        chunks = [c.audio_int16_array for c in self.voice.synthesize(text, syn_config=self.syn)]
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(chunks + [self.pause]).astype(np.int16)

    async def synthesize(self, text: str) -> np.ndarray:
        return await asyncio.get_running_loop().run_in_executor(_tts_pool, self._run, text)


_SENT_RE = re.compile(r"(?<=[.!?;:…])\s+|\n+")


_EMOJI_RE = re.compile(
    "[🀀-🫿☀-➿🇦-🇿‍️⭐⭕←-⇿]+"
)


def clean_text(text: str) -> str:
    """Toglie emoji e markdown: la risposta viene letta ad alta voce."""
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[*_#`>]+", "", text)
    return re.sub(r"[ 	]{2,}", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    """Spezza il testo in frasi brevi da leggere una alla volta."""
    text = clean_text(text)
    parts = [p.strip() for p in _SENT_RE.split(text) if p and p.strip()]
    return [p for p in parts if re.search(r"\w", p)]
