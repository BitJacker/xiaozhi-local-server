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


LATIN_LANGS = ("it", "en", "fr", "de", "es")
# frasi tipiche che Whisper "inventa" sul rumore o sul silenzio (vengono dai sottotitoli dei video)
HALLUCINATION_RE = re.compile(r"(iscriviti al canale|sottotitoli (creati|a cura|di)|amara\.org|grazie per la visione|"
                              r"grazie per l'ascolto|subscribe|like and subscribe|thanks for watching|"
                              r"sous-titr|untertitel|subt[ií]tulos? (realizados|por))", re.I)


def _foreign_ratio(text: str) -> float:
    """Quante lettere non appartengono alle lingue europee occidentali (es. vietnamita, cirillico, cinese)."""
    letters = [c for c in text if c.isalpha()]
    return sum(ord(c) > 0x17F for c in letters) / len(letters) if letters else 0.0


class ASR:
    def __init__(self, cfg: dict):
        _add_cuda_dlls()
        self.cfg = cfg
        self.language = cfg.get("language", "it")
        self.prompt = cfg.get("prompt") or None
        self.beam_size = int(cfg.get("beam_size", 5))
        self.model, self.model_name, self.device = self._load(cfg.get("model", "small"))

    def _load(self, model: str):
        from faster_whisper import WhisperModel
        cfg = self.cfg
        device = cfg.get("device", "auto")
        compute = cfg.get("compute_type", "auto")
        if device == "auto" and not cuda_available():
            device = "cpu"
        if device == "cpu" and model in ("medium", "large-v3", "large-v3-turbo"):
            # senza GPU i modelli grandi sono troppo lenti: si usa quello per CPU
            model, compute = cfg.get("cpu_model", "small"), "int8"
        elif device == "cpu":
            compute = "int8"
        try:
            m = WhisperModel(model, device=device, compute_type=compute, num_workers=2)
            # Prova reale: su alcune GPU mancano le librerie CUDA e l'errore emerge solo qui.
            noise = (np.random.default_rng(0).standard_normal(16000) * 0.1).astype(np.float32)
            segs, _ = m.transcribe(noise, language=self.language)
            list(segs)  # i segmenti sono pigri: va consumato per eseguire davvero il modello
            log.info("Whisper '%s' caricato (device=%s, %s)", model, m.model.device, compute)
            return m, model, str(m.model.device)
        except Exception as e:  # fallback CPU
            log.warning("Whisper su %s non disponibile (%s): uso CPU", device, e)
            cpu_model = cfg.get("cpu_model", "small")
            m = WhisperModel(cpu_model, device="cpu", compute_type="int8", num_workers=2)
            log.info("Whisper '%s' caricato su CPU", cpu_model)
            return m, cpu_model, "cpu"

    async def reload(self, model: str):
        """Carica un altro modello (dalla pagina Admin) e lo mette al posto del vecchio solo quando è pronto:
        intanto le domande continuano a usare quello di prima."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _download_whisper, model)
        m, name, device = await loop.run_in_executor(None, self._load, model)
        self.model, self.model_name, self.device = m, name, device


    def _run(self, pcm: np.ndarray, language: str | None = None) -> str:
        audio = pcm.astype(np.float32) / 32768.0
        lang = language or self.language
        segments, _ = self.model.transcribe(
            audio, language=None if lang == "auto" else lang, beam_size=self.beam_size, vad_filter=True,
            condition_on_previous_text=False, initial_prompt=self.prompt if lang == self.language else None,
        )
        # Sul rumore Whisper a volte "inventa" frasi (es. "iscriviti al canale"): si scartano i pezzi che lui stesso
        # giudica probabilmente non parlato, o molto incerti, o ripetitivi.
        kept = [s for s in segments
                if not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.5)
                and s.avg_logprob > -1.2 and s.compression_ratio < 2.6]
        text = " ".join(s.text.strip() for s in kept).strip()
        if text and (HALLUCINATION_RE.search(text) or (lang in LATIN_LANGS and _foreign_ratio(text) > 0.05)):
            log.info("Trascrizione scartata (probabile frase inventata sul rumore): %s", text[:80])
            return ""
        return text

    async def transcribe(self, pcm: np.ndarray, language: str | None = None) -> str:
        """language: quella di config (predefinita), un'altra (es. "en" per il tutor di inglese) o "auto"."""
        return await asyncio.get_running_loop().run_in_executor(_asr_pool, self._run, pcm, language)


def _download_whisper(model: str):
    """Scarica il modello se manca. Di solito il server lavora offline (HF_HUB_OFFLINE): qui, e solo perché l'admin
    l'ha chiesto dalla pagina, si permette lo scaricamento da Hugging Face."""
    from faster_whisper import download_model
    try:
        download_model(model, local_files_only=True)
        return
    except Exception:
        pass
    import huggingface_hub.constants as hf_constants
    old = hf_constants.HF_HUB_OFFLINE
    hf_constants.HF_HUB_OFFLINE = False
    try:
        log.info("Scarico il modello Whisper '%s' (richiesto dalla pagina Admin)...", model)
        download_model(model)
    finally:
        hf_constants.HF_HUB_OFFLINE = old


class TTS:
    def __init__(self, cfg: dict):
        from piper import PiperVoice
        from piper.config import SynthesisConfig

        self.voice = PiperVoice.load(cfg["voice"])
        # la frequenza resta quella della prima voce anche se poi l'admin cambia voce: i dispositivi e le pagine già
        # collegati la conoscono dal saluto iniziale
        self.sample_rate = self.native_rate = self.voice.config.sample_rate
        self.syn = SynthesisConfig(
            length_scale=float(cfg.get("length_scale", 1.0)),
            noise_scale=cfg.get("noise_scale"),
            noise_w_scale=cfg.get("noise_w_scale"),
        )
        # pausa di silenzio aggiunta alla fine di ogni frase (respiro naturale)
        self.pause = np.zeros(int(self.sample_rate * cfg.get("sentence_pause_ms", 0) / 1000), dtype=np.int16)
        log.info("Voce Piper caricata: %s (%d Hz)", cfg["voice"], self.sample_rate)

    def _run(self, text: str) -> np.ndarray:
        voice, rate = self.voice, self.native_rate
        chunks = [c.audio_int16_array for c in voice.synthesize(text, syn_config=self.syn)]
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        pcm = np.concatenate(chunks).astype(np.int16)
        if rate != self.sample_rate:
            from .audio import resample
            pcm = resample(pcm, rate, self.sample_rate)
        return np.concatenate([pcm, self.pause]).astype(np.int16)

    def load_voice(self, path: str):
        """Cambia la voce predefinita senza riavviare (pagina Admin → Modelli)."""
        from piper import PiperVoice
        voice = PiperVoice.load(path)
        self.voice, self.native_rate = voice, voice.config.sample_rate
        log.info("Voce predefinita: %s", path)

    async def synthesize(self, text: str) -> np.ndarray:
        return await asyncio.get_running_loop().run_in_executor(_tts_pool, self._run, text)


class PersonaVoice:
    """Un'altra voce (o velocità) per un personaggio, con la stessa frequenza della voce principale: così i
    dispositivi e la pagina web non devono cambiare formato a metà conversazione."""

    def __init__(self, tts: "TTS", base_rate: int):
        self.tts, self.sample_rate = tts, base_rate

    async def synthesize(self, text: str) -> np.ndarray:
        from .audio import resample
        pcm = await self.tts.synthesize(text)
        return pcm if self.tts.sample_rate == self.sample_rate else resample(pcm, self.tts.sample_rate, self.sample_rate)


class VoicePool:
    """Voci dei personaggi, caricate la prima volta che servono (cartella models/piper)."""

    def __init__(self, base: TTS, cfg: dict):
        self.base, self.cfg = base, cfg
        self.folder = os.path.dirname(cfg["voice"]) or "."
        self._cache: dict[tuple[str, float], PersonaVoice] = {}

    def clear(self):
        self._cache.clear()

    def available(self) -> list[str]:
        try:
            return sorted(f for f in os.listdir(self.folder) if f.endswith(".onnx"))
        except OSError:
            return []

    def get(self, voice: str = "", speed: float | None = None):
        name = os.path.basename(voice or "")
        if (not name or name == os.path.basename(self.cfg["voice"])) and speed in (None, self.cfg.get("length_scale")):
            return self.base
        if name and name not in self.available():
            name = ""
        key = (name, float(speed or self.cfg.get("length_scale", 1.0)))
        if key not in self._cache:
            cfg = {**self.cfg, "length_scale": key[1]}
            if name:
                cfg["voice"] = os.path.join(self.folder, name)
            self._cache[key] = PersonaVoice(TTS(cfg), self.base.sample_rate)
        return self._cache[key]


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
