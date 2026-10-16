"""Cifratura dei dati salvati su disco (cronologia, segreti 2FA) con AES-256-GCM.

La chiave è in un file a parte (security.key_file, predefinito data/encryption.key). Protegge i dati se
qualcuno copia i file della cronologia o users.json senza la chiave; per proteggere anche dal furto del
disco intero conviene la cifratura del disco (BitLocker su Windows, LUKS su Linux) oppure tenere la chiave
su un supporto diverso (security.key_file).
"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc1:"


class Box:
    def __init__(self, key_path: Path, enabled: bool = True):
        self.enabled = enabled
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            key_path.write_bytes(AESGCM.generate_key(bit_length=256))
            try:
                os.chmod(key_path, 0o600)  # leggibile solo dall'utente del server (su Linux)
            except OSError:
                pass
        self._aes = AESGCM(key_path.read_bytes()[:32])

    def encrypt(self, text: str) -> str:
        if not self.enabled:
            return text
        nonce = secrets.token_bytes(12)
        return PREFIX + base64.b64encode(nonce + self._aes.encrypt(nonce, text.encode("utf-8"), None)).decode()

    def decrypt(self, value: str) -> str:
        """Accetta anche valori in chiaro (dati salvati prima di attivare la cifratura)."""
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        raw = base64.b64decode(value[len(PREFIX):])
        return self._aes.decrypt(raw[:12], raw[12:], None).decode("utf-8")


def from_config(cfg: dict, data_dir: Path) -> Box:
    from .config import ROOT
    sec = cfg.get("security") or {}
    key = Path(sec["key_file"]) if sec.get("key_file") else Path(data_dir) / "encryption.key"
    if not key.is_absolute():
        key = ROOT / key
    return Box(key, bool(sec.get("encrypt_data", True)))
