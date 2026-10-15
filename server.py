import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# I modelli Whisper vengono scaricati dentro il progetto (models/hf), non nel profilo utente.
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Se i modelli sono già stati scaricati (install.sh / install.ps1) si lavora offline:
# nessuna connessione a Internet, nemmeno per controllare aggiornamenti dei modelli.
if (ROOT / "models" / "hf" / "hub").is_dir():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

from xiaozhi_locale.app import main  # noqa: E402

if __name__ == "__main__":
    main()
