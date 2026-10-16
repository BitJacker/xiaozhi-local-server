"""Scarica i modelli vocali una volta sola (poi il server lavora offline).

Uso:  python scripts/download_models.py [--gpu]
  --gpu   scarica anche il modello Whisper grande (per schede NVIDIA)
"""
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
os.environ["HF_HOME"] = str(ROOT / "models" / "hf")
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

sys.path.insert(0, str(ROOT))
from xiaozhi_locale.config import load  # noqa: E402

cfg = load()  # impostazioni predefinite + le tue (data/config.yaml, es. profilo leggero)
gpu = "--gpu" in sys.argv

# --- voce Piper
voice_path = ROOT / cfg["tts"]["voice"]
if not voice_path.exists():
    print(f"Scarico la voce {voice_path.stem}...", flush=True)
    voice_path.parent.mkdir(parents=True, exist_ok=True)
    from piper.download_voices import download_voice
    download_voice(voice_path.stem, voice_path.parent)
print(f"Voce: {voice_path.name} OK", flush=True)

# --- Whisper
from faster_whisper import download_model  # noqa: E402

models = [cfg["asr"].get("cpu_model", "small")]
if gpu:
    models.insert(0, cfg["asr"].get("model", "large-v3-turbo"))
for m in dict.fromkeys(models):
    print(f"Scarico Whisper '{m}' (se manca)...", flush=True)
    download_model(m)
    print(f"Whisper '{m}' OK", flush=True)
