"""Configurazione a due livelli.

* `config.yaml` (nella cartella del programma): impostazioni PREDEFINITE. Gli aggiornamenti lo sostituiscono.
* `data/config.yaml`: le TUE impostazioni. Gli aggiornamenti non lo toccano mai.

Il risultato è `config.yaml` con sopra le voci di `data/config.yaml` (le liste vengono sostituite, non unite).
Tutti i dati dell'utente vivono in `data/`: impostazioni, utenti, dispositivi, sessioni, cronologia.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 2  # formato della cartella data/ (vedi migrate)

USER_CONFIG_TEMPLATE = """\
# LE TUE IMPOSTAZIONI (non vengono mai toccate dagli aggiornamenti).
# Scrivi qui solo le voci che vuoi cambiare rispetto a config.yaml, con la stessa struttura.
# Esempio:
#
# mcp_servers:
#   - name: huskylens
#     url: http://192.168.1.50:3000/sse
#
# auth:
#   require_2fa: true
"""


ENV_OVERRIDES = {
    "HUSKY_LLM_URL": ("llm", "base_url"),         # es. http://ollama:11434/v1 in docker compose
    "HUSKY_LLM_MODEL": ("llm", "model"),
    "HUSKY_PUBLIC_HOST": ("server", "public_host"),  # IP del PC nella LAN (dentro un container non si vede)
    "HUSKY_WHISPER": ("asr", "model"),            # es. small su un server senza scheda video
}


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path: str | os.PathLike | None = None) -> dict:
    defaults_path = Path(path) if path else ROOT / "config.yaml"
    base = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
    data_dir = Path(os.environ.get("HUSKY_DATA") or base.get("data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    migrate(data_dir)
    user_path = data_dir / "config.yaml"
    if not user_path.exists():
        user_path.write_text(USER_CONFIG_TEMPLATE, encoding="utf-8")
    user = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
    cfg = deep_merge(base, user)
    # Variabili d'ambiente (Docker, Kubernetes): valgono sopra i file
    for var, (section, key) in ENV_OVERRIDES.items():
        if os.environ.get(var):
            cfg.setdefault(section, {})[key] = os.environ[var]
            if var == "HUSKY_WHISPER":  # vale anche senza scheda video
                cfg["asr"]["cpu_model"] = os.environ[var]
    cfg["_data_dir"] = str(data_dir)
    cfg["_version"] = version()
    return cfg


def migrate(data_dir: Path):
    """Aggiorna il formato della cartella data/ tra una versione e l'altra, senza perdere niente."""
    meta_path = data_dir / "schema.json"
    try:
        current = json.loads(meta_path.read_text(encoding="utf-8")).get("version", 1)
    except (OSError, ValueError):
        current = 1
    if current < 2:
        # v1 -> v2: nessun dato da convertire (la cronologia era già in data/history);
        # da qui in poi utenti, dispositivi e sessioni vivono in data/.
        pass
    if current != SCHEMA_VERSION:
        meta_path.write_text(json.dumps({"version": SCHEMA_VERSION}), encoding="utf-8")
