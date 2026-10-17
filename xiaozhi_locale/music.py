"""Musica e audio dalla cartella del server (es. canzoni per la lezione di inglese, suoni, registrazioni).

L'admin carica i file dalla pagina Admin (sezione Musica) oppure li copia nella cartella data/music. A voce: "metti
la canzone Yesterday", "che musica c'è?". Husky risponde e poi suona il file sul dispositivo (o nella pagina web);
la parola di attivazione o il pulsante la fermano. Spenta di serie: si accende dalla pagina Admin.
Formati: mp3, ogg, opus, wav, flac, m4a (decodificati sul server con FFmpeg di PyAV, già presente).
"""
from __future__ import annotations

import logging
import random
import re
import threading
import unicodedata
from pathlib import Path

import numpy as np

from .tools import ToolSet

log = logging.getLogger("music")
EXTS = (".mp3", ".ogg", ".opus", ".wav", ".flac", ".m4a")
MAX_FILE = 60 * 1024 * 1024
NAME_RE = re.compile(r"^[\w\-. ()'&,!]{1,120}$", re.UNICODE)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def safe_name(filename: str) -> str:
    """Nome di file sicuro: niente cartelle, niente caratteri strani, solo le estensioni audio ammesse."""
    from urllib.parse import unquote
    name = Path(unquote(str(filename)).replace("\\", "/")).name.strip()
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^\w\-. ()'&,!]", "_", stem, flags=re.UNICODE).strip(" .")[:100]
    if ext not in EXTS:
        raise ValueError("Formato non supportato (mp3, ogg, opus, wav, flac, m4a)")
    if not stem:
        raise ValueError("Nome del file non valido")
    return stem + ext


class Music:
    def __init__(self, data_dir: Path, cfg: dict):
        self.cfg = cfg  # sezione music (enabled, roles, folder, max_minutes)
        from .config import ROOT
        folder = Path(cfg.get("folder") or Path(data_dir) / "music")
        self.dir = (folder if folder.is_absolute() else ROOT / folder).resolve()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def roles(self) -> list[str]:
        return list(self.cfg.get("roles") or ["admin", "user", "device"])

    def allowed(self, role: str) -> bool:
        return self.enabled and role in self.roles()

    def items(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.iterdir(), key=lambda p: p.name.lower()):
            if p.is_file() and p.suffix.lower() in EXTS:
                out.append({"name": p.name, "title": p.stem.replace("_", " "), "size": p.stat().st_size})
        return out

    def path(self, name: str) -> Path | None:
        name = Path(str(name)).name
        p = (self.dir / name).resolve()
        if p.parent != self.dir or not p.is_file() or p.suffix.lower() not in EXTS:
            return None
        return p

    def save(self, filename: str, data: bytes) -> dict:
        name = safe_name(filename)
        if len(data) > MAX_FILE:
            raise ValueError("File troppo grande (massimo 60 MB)")
        if len(data) < 256:
            raise ValueError("File vuoto")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (name + ".part")
        tmp.write_bytes(data)
        try:
            dur = probe_seconds(tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise ValueError("Il file non sembra un audio valido")
        tmp.replace(self.dir / name)
        return {"name": name, "title": Path(name).stem.replace("_", " "), "seconds": round(dur)}

    def delete(self, name: str) -> bool:
        p = self.path(name)
        if p is None:
            return False
        p.unlink()
        return True

    def find(self, query: str) -> dict | None:
        items = self.items()
        if not items:
            return None
        q = _norm(query)
        if not q or q in ("casuale", "a caso", "qualsiasi", "una canzone", "musica"):
            return random.choice(items)
        words = set(q.split())
        best, score = None, 0.0
        for it in items:
            t = _norm(it["title"])
            if q in t:
                return it
            common = len(words & set(t.split()))
            s = common / max(1, len(words))
            if s > score:
                best, score = it, s
        return best if score >= 0.5 else None

    def toolset(self, session) -> ToolSet:
        ts = ToolSet()

        async def list_(_n, _a):
            items = self.items()
            if not items:
                return "Nella cartella della musica non c'è ancora niente."
            names = [it["title"] for it in items[:40]]
            return f"Brani disponibili ({len(items)}): " + "; ".join(names)

        async def play(_n, a):
            it = self.find(str(a.get("titolo", "")))
            if it is None:
                return "Non trovo un brano così. Usa musica_elenco per sapere cosa c'è."
            session.pending_music = self.dir / it["name"]
            return (f"Ok: appena finisci di parlare parte '{it['title']}'. Annuncialo in una frase breve, senza "
                    f"aggiungere altro.")
        titles = [it["title"] for it in self.items()]
        some = "; ".join(titles[:15]) + ("…" if len(titles) > 15 else "")
        ts.add("musica_elenco", "Elenca la musica e gli audio presenti sul server.",
               {"type": "object", "properties": {}}, list_)
        ts.add("musica_riproduci", "Fa partire subito un brano (canzone, audio, suono) della cartella del server. Usalo "
               "quando l'utente chiede di mettere, suonare, far sentire o ascoltare qualcosa; per \"metti un po' di "
               "musica\" usa titolo=casuale. "
               + (f"Brani disponibili: {some}." if titles else "Al momento non ci sono brani."),
               {"type": "object", "properties": {"titolo": {"type": "string", "maxLength": 120,
                                                            "description": "titolo o parole del titolo; 'casuale' per uno a caso"}},
                "required": ["titolo"]}, play)
        return ts


def probe_seconds(path: Path) -> float:
    import av
    with av.open(str(path)) as c:
        st = next(s for s in c.streams if s.type == "audio")
        if c.duration:
            return c.duration / 1_000_000
        if st.duration and st.time_base:
            return float(st.duration * st.time_base)
        return 0.0


def decode_chunks(path: Path, rate: int, stop: threading.Event, max_seconds: float, chunk_s: float = 1.0):
    """Decodifica a pezzi (mono, int16, alla frequenza voluta) senza caricare tutto il file in memoria."""
    import av
    with av.open(str(path)) as c:
        st = next(s for s in c.streams if s.type == "audio")
        res = av.AudioResampler(format="s16", layout="mono", rate=rate)
        buf, total, step = [], 0, int(rate * chunk_s)
        for frame in c.decode(st):
            if stop.is_set():
                return
            for rf in res.resample(frame):
                buf.append(rf.to_ndarray().reshape(-1))
            n = sum(len(b) for b in buf)
            if n >= step:
                pcm = np.concatenate(buf).astype(np.int16)
                buf = []
                total += len(pcm)
                yield pcm
                if total >= max_seconds * rate:
                    return
        for rf in res.resample(None):
            buf.append(rf.to_ndarray().reshape(-1))
        if buf:
            yield np.concatenate(buf).astype(np.int16)
