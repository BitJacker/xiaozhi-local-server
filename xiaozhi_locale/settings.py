"""Impostazioni cambiate dalla pagina Admin, senza toccare i file di configurazione.

Sono salvate in data/settings.json e valgono sopra config.yaml e data/config.yaml (come se fossero scritte lì):
così l'admin può cambiare dal browser, ad esempio, per quanti giorni tenere la cronologia, il modello linguistico, il
modello di Whisper, la voce predefinita o il rilevamento della fine della frase. Solo le voci elencate in ALLOWED si
possono cambiare così, ognuna con il suo controllo.
"""
from __future__ import annotations

import re
from pathlib import Path

from .auth import JsonStore

MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-/]{0,120}(:[a-zA-Z0-9._\-]{1,80})?$")
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3-turbo", "large-v3")
VOICE_RE = re.compile(r"^[a-zA-Z0-9_\-]{3,80}\.onnx$")


def _days(v):
    v = int(v)
    if v not in (0, 7, 14, 30, 60, 90, 180, 365):
        raise ValueError("Durata della cronologia non valida")
    return v


def _model(v):
    v = str(v).strip()
    if not MODEL_RE.match(v):
        raise ValueError("Nome del modello non valido")
    return v


def _whisper(v):
    if v not in WHISPER_MODELS:
        raise ValueError("Modello di Whisper non valido")
    return v


def _engine(v):
    if v not in ("silero", "energy"):
        raise ValueError("Rilevamento della fine della frase non valido")
    return v


def _silence(v):
    v = int(v)
    if not 300 <= v <= 3000:
        raise ValueError("Il silenzio di fine frase va da 300 a 3000 ms")
    return v


def _bool(v):
    return bool(v)


def _threshold(v):
    v = round(float(v), 2)
    if not 0.3 <= v <= 0.95:
        raise ValueError("La soglia va da 0,30 a 0,95")
    return v


def _roles(v):
    if not isinstance(v, list) or not all(r in ("admin", "user", "device") for r in v):
        raise ValueError("Ruoli non validi")
    return sorted(set(v))


def _voice(v):
    v = Path(str(v)).name
    if not VOICE_RE.match(v):
        raise ValueError("Voce non valida")
    return v


# chiave "sezione.voce" -> controllo del valore
ALLOWED = {
    "history.keep_days": _days,
    "llm.model": _model,
    "asr.model": _whisper,
    "tts.voice": _voice,
    "vad.engine": _engine,
    "vad.silence_ms": _silence,
    "voiceprint.enabled": _bool,
    "voiceprint.threshold": _threshold,
    "music.enabled": _bool,
    "music.roles": _roles,
    "metrics.enabled": _bool,
}


class Settings:
    def __init__(self, data_dir: Path, cfg: dict):
        self.cfg = cfg
        self.store = JsonStore(Path(data_dir) / "settings.json")
        for key, value in list(self.store.data.items()):
            try:
                self._apply(key, ALLOWED[key](value))
            except (KeyError, ValueError, TypeError):
                self.store.data.pop(key, None)  # voce sconosciuta o non più valida: si ignora

    def _apply(self, key: str, value):
        section, name = key.split(".")
        if key == "tts.voice":  # la voce sta nella cartella delle voci, accanto a quella predefinita
            value = str(Path(self.cfg["tts"]["voice"]).parent / value)
        self.cfg.setdefault(section, {})[name] = value

    def get(self, key: str, default=None):
        section, name = key.split(".")
        value = (self.cfg.get(section) or {}).get(name, default)
        return Path(value).name if key == "tts.voice" and value else value

    def set(self, key: str, value):
        if key not in ALLOWED:
            raise ValueError("Impostazione sconosciuta")
        try:
            value = ALLOWED[key](value)
        except (TypeError, ValueError) as e:
            raise ValueError(str(e) if str(e) and not str(e).startswith(("invalid", "could not")) else "Valore non valido")
        with self.store.lock:
            self.store.data[key] = value
            self.store.save()
        self._apply(key, value)
        return value
