"""Codifica/decodifica Opus (via PyAV, che include libopus) e VAD a energia."""
from __future__ import annotations

import fractions

import av
import numpy as np


class OpusDecoder:
    """Decodifica pacchetti Opus grezzi (protocollo xiaozhi v1) in PCM int16 mono 16 kHz."""

    def __init__(self, sample_rate: int = 16000):
        self.ctx = av.CodecContext.create("libopus", "r")
        self.ctx.sample_rate = sample_rate
        self.ctx.layout = "mono"
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    def decode(self, packet: bytes) -> np.ndarray:
        out = []
        try:
            frames = self.ctx.decode(av.Packet(packet))
        except av.error.FFmpegError:
            return np.zeros(0, dtype=np.int16)
        for frame in frames:
            for rf in self.resampler.resample(frame):
                out.append(rf.to_ndarray().reshape(-1))
        return np.concatenate(out).astype(np.int16) if out else np.zeros(0, dtype=np.int16)


class OpusEncoder:
    """Codifica PCM int16 mono in pacchetti Opus da `frame_ms` (default 60 ms)."""

    def __init__(self, sample_rate: int = 24000, frame_ms: int = 60):
        self.sample_rate = sample_rate
        self.frame_samples = sample_rate * frame_ms // 1000
        self.ctx = av.CodecContext.create("libopus", "w")
        self.ctx.sample_rate = sample_rate
        self.ctx.layout = "mono"
        self.ctx.format = "s16"
        self.ctx.bit_rate = 32000
        self.ctx.time_base = fractions.Fraction(1, sample_rate)
        self.ctx.options = {"frame_duration": str(frame_ms), "application": "voip"}
        self.pts = 0

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        """Restituisce un pacchetto per ogni frame completo; l'ultimo frame viene riempito di silenzio."""
        n = self.frame_samples
        if len(pcm) % n:
            pcm = np.concatenate([pcm, np.zeros(n - len(pcm) % n, dtype=np.int16)])
        packets: list[bytes] = []
        for i in range(0, len(pcm), n):
            frame = av.AudioFrame.from_ndarray(pcm[i:i + n].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = self.sample_rate
            frame.pts = self.pts
            frame.time_base = self.ctx.time_base
            self.pts += n
            packets.extend(bytes(p) for p in self.ctx.encode(frame))
        return packets


def encode_ogg_opus(pcm: np.ndarray, sample_rate: int) -> bytes:
    """File Ogg Opus mono (per le notifiche vocali dei dispositivi xiaozhi: lo scaricano e lo riproducono)."""
    import io

    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="ogg")
    st = out.add_stream("libopus", rate=48000)
    st.layout = "mono"
    st.bit_rate = 32000
    st.options = {"frame_duration": "60", "application": "voip"}
    pcm48 = resample(pcm, sample_rate, 48000)
    step = 2880  # 60 ms a 48 kHz
    for i in range(0, len(pcm48), step):
        chunk = pcm48[i:i + step]
        if len(chunk) < step:
            chunk = np.concatenate([chunk, np.zeros(step - len(chunk), dtype=np.int16)])
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = i
        frame.time_base = fractions.Fraction(1, 48000)
        for p in st.encode(frame):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


def resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or len(pcm) == 0:
        return pcm
    n_out = int(len(pcm) * dst_rate / src_rate)
    x = np.linspace(0, len(pcm) - 1, n_out)
    return np.interp(x, np.arange(len(pcm)), pcm.astype(np.float32)).astype(np.int16)


class EnergyVAD:
    """VAD semplice a energia: segnala la fine della frase dopo `silence_ms` di silenzio."""

    def __init__(self, silence_ms: int, min_speech_ms: int, threshold: float, max_utterance_s: float):
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.threshold = threshold
        self.max_samples = int(max_utterance_s * 16000)
        self.reset()

    def reset(self):
        self.speech_ms = 0.0
        self.silence_run_ms = 0.0
        self.started = False
        self.samples = 0

    def feed(self, pcm: np.ndarray) -> bool:
        """Ritorna True quando l'utente ha finito di parlare."""
        if len(pcm) == 0:
            return False
        ms = len(pcm) / 16.0
        self.samples += len(pcm)
        rms = float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))
        if rms >= self.threshold:
            self.speech_ms += ms
            self.silence_run_ms = 0
            if self.speech_ms >= self.min_speech_ms:
                self.started = True
        else:
            self.silence_run_ms += ms
        if self.started and self.silence_run_ms >= self.silence_ms:
            return True
        return self.started and self.samples >= self.max_samples
