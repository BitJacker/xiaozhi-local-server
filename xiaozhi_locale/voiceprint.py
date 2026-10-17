"""Impronta vocale (facoltativa, solo con il consenso della persona): i dispositivi condivisi della classe capiscono
chi sta parlando e usano i suoi ricordi.

* Spenta di serie: la accende l'admin (pagina Admin → Impostazioni) dopo aver scaricato il modello (26 MB, WeSpeaker
  ResNet34, licenza CC BY 4.0) che gira sul processore del server.
* Ognuno registra la PROPRIA voce dalla pagina Account, dopo aver letto e accettato il consenso; può cancellarla
  quando vuole. L'admin non può registrare la voce di qualcun altro.
* Si salva solo un'impronta numerica (256 numeri), cifrata in data/voiceprints.json: non la registrazione.
* Riconoscere la voce NON dà permessi: una voce si può imitare o registrare. Serve solo a usare i ricordi della
  persona, a chiamarla per nome e a mettere la conversazione nella sua cronologia.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import numpy as np

from .auth import JsonStore

log = logging.getLogger("voiceprint")
MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
MODEL_SIZE = 26530309
MIN_SECONDS = 1.0        # sotto questa durata la voce non si riconosce in modo affidabile
ENROLL_SECONDS = 8.0     # voce necessaria per registrare l'impronta (in totale)
CONSENT_VERSION = 1


def fbank(pcm16: np.ndarray, sr: int = 16000, n_mels: int = 80) -> np.ndarray:
    """Banchi di filtri mel in stile Kaldi (25 ms, passo 10 ms), come vuole il modello; media tolta."""
    x = pcm16.astype(np.float32)
    fl, fs, nfft = 400, 160, 512
    n = 1 + (len(x) - fl) // fs
    if n <= 0:
        return np.zeros((0, n_mels), np.float32)
    idx = np.arange(fl)[None, :] + fs * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1], fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    fr *= (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(fl) / (fl - 1))) ** 0.85
    spec = np.abs(np.fft.rfft(fr, nfft)) ** 2
    feats = np.log(np.maximum(spec @ _mel_bank(sr, nfft, n_mels).T, np.finfo(np.float32).eps))
    return (feats - feats.mean(axis=0)).astype(np.float32)


def voiced(pcm16: np.ndarray) -> np.ndarray:
    """Solo i pezzi di 20 ms con la voce: le pause e il silenzio prima e dopo la frase confondono l'impronta (provato:
    con la voce passata per Opus la somiglianza della stessa persona sale da circa 0,5 a 0,6-0,8)."""
    n = len(pcm16) // 320 * 320
    if n == 0:
        return pcm16
    f = pcm16[:n].reshape(-1, 320).astype(np.float32)
    rms = np.sqrt((f ** 2).mean(axis=1))
    keep = rms > max(rms.max() * 0.05, 100)
    return f[keep].reshape(-1).astype(np.int16) if keep.sum() >= 25 else pcm16


_BANKS: dict = {}


def _mel_bank(sr: int, nfft: int, n_mels: int) -> np.ndarray:
    key = (sr, nfft, n_mels)
    if key not in _BANKS:
        mel = lambda f: 1127 * np.log(1 + f / 700)  # noqa: E731
        pts = np.linspace(mel(20), mel(sr / 2), n_mels + 2)
        freqs = mel(np.arange(nfft // 2 + 1) * sr / nfft)
        fb = np.zeros((n_mels, nfft // 2 + 1), np.float32)
        for m in range(n_mels):
            lo, c, hi = pts[m], pts[m + 1], pts[m + 2]
            fb[m] = np.maximum(0, np.minimum((freqs - lo) / (c - lo), (hi - freqs) / (hi - c)))
        _BANKS[key] = fb
    return _BANKS[key]


class Voiceprints:
    def __init__(self, data_dir: Path, box, cfg: dict, models_dir: Path):
        self.cfg = cfg  # sezione voiceprint della configurazione (enabled, threshold)
        self.store = JsonStore(Path(data_dir) / "voiceprints.json")
        self.box = box
        self.model_path = Path(models_dir) / "voiceprint" / "wespeaker-resnet34.onnx"
        self._session = None
        self.download: dict | None = None

    # ------------------------------------------------------------------ stato
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def threshold(self) -> float:
        return float(self.cfg.get("threshold", 0.55))

    def ready(self) -> bool:
        return self.enabled and self.model_path.exists()

    def status(self) -> dict:
        return {"enabled": self.enabled, "model": self.model_path.exists(), "threshold": self.threshold,
                "enrolled": len(self.store.data), "download": self.download}

    # ------------------------------------------------------------------ modello
    async def download_model(self):
        import aiohttp
        if self.download and self.download.get("state") == "running":
            raise ValueError("Lo sto già scaricando")
        self.download = job = {"state": "running", "percent": 0, "status": "scarico"}
        tmp = self.model_path.with_suffix(".part")
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as http:
                async with http.get(MODEL_URL) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Hugging Face ha risposto {r.status}")
                    done = 0
                    with open(tmp, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
                            done += len(chunk)
                            job["percent"] = min(99, round(100 * done / MODEL_SIZE))
            if tmp.stat().st_size < 1_000_000:
                raise RuntimeError("file incompleto")
            os.replace(tmp, self.model_path)
            job.update(state="done", percent=100, status="pronto")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            job.update(state="error", status=str(e)[:200])

    def _model(self):
        if self._session is None:
            import onnxruntime
            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = 2
            opts.log_severity_level = 4
            self._session = onnxruntime.InferenceSession(str(self.model_path), providers=["CPUExecutionProvider"],
                                                         sess_options=opts)
        return self._session

    def embed(self, pcm16: np.ndarray) -> np.ndarray:
        feats = fbank(voiced(pcm16))
        s = self._model()
        e = s.run(None, {s.get_inputs()[0].name: feats[None]})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)

    # ------------------------------------------------------------------ impronte salvate
    def _load(self, user: str) -> dict | None:
        raw = self.store.data.get(user)
        if not raw:
            return None
        try:
            return json.loads(self.box.decrypt(raw) if self.box else raw)
        except Exception:
            return None

    def info(self, user: str) -> dict:
        rec = self._load(user)
        return {"enabled": self.ready(), "enrolled": rec is not None,
                "created": rec.get("created", "") if rec else "", "seconds": ENROLL_SECONDS}

    async def enroll(self, user: str, samples: list[np.ndarray], consent: bool) -> dict:
        if not self.ready():
            raise ValueError("L'impronta vocale non è attiva su questo server")
        if not consent:
            raise ValueError("Serve il tuo consenso esplicito")
        voiced = [s for s in samples if len(s) >= 16000 * MIN_SECONDS]
        if sum(len(s) for s in voiced) < 16000 * ENROLL_SECONDS * 0.75:
            raise ValueError("Registrazione troppo breve: leggi tutte le frasi")
        if any(float(np.sqrt(np.mean((s.astype(np.float32) / 32768) ** 2))) < 0.005 for s in voiced):
            raise ValueError("Non sento la voce: controlla il microfono")
        loop = asyncio.get_running_loop()
        embs = [await loop.run_in_executor(None, self.embed, s) for s in voiced]
        mean = np.mean(embs, axis=0)
        mean /= np.linalg.norm(mean) + 1e-9
        rec = {"emb": [round(float(x), 6) for x in mean], "created": time.strftime("%d/%m/%Y %H:%M"),
               "consent": CONSENT_VERSION}
        raw = json.dumps(rec)
        with self.store.lock:
            self.store.data[user] = self.box.encrypt(raw) if self.box else raw
            self.store.save()
        log.info("Impronta vocale registrata per %s (con consenso)", user)
        return self.info(user)

    def delete(self, user: str) -> bool:
        with self.store.lock:
            found = self.store.data.pop(user, None) is not None
            self.store.save()
        return found

    async def identify(self, pcm16: np.ndarray) -> tuple[str | None, float]:
        """Chi sta parlando, tra chi ha registrato la voce; None se nessuno è abbastanza simile (o se due
        persone si somigliano troppo: meglio non indovinare)."""
        if not self.ready() or not self.store.data or len(pcm16) < 16000 * MIN_SECONDS:
            return None, 0.0
        e = await asyncio.get_running_loop().run_in_executor(None, self.embed, pcm16)
        scores = []
        for user in list(self.store.data):
            rec = self._load(user)
            if rec:
                scores.append((float(np.dot(e, np.asarray(rec["emb"], np.float32))), user))
        if not scores:
            return None, 0.0
        scores.sort(reverse=True)
        best, user = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        if best >= self.threshold and best - second >= 0.08:
            return user, best
        return None, best
