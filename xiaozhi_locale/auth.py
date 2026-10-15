"""Utenti, password, 2FA (TOTP), sessioni, dispositivi approvati e limiti anti-spam.

Tutto è salvato in data/ (users.json, sessions.json, devices.json, secret.key) e resta sul server.
- Password: hash scrypt con sale casuale (mai salvate in chiaro).
- 2FA: TOTP standard RFC 6238 (Google Authenticator, Aegis, Microsoft Authenticator...), anti-riuso del codice.
- Sessioni: token casuale nel cookie; sul disco se ne salva solo l'hash SHA-256.
- Dispositivi xiaozhi: si attivano con un codice a 6 cifre approvato dall'admin; il token del dispositivo
  è un HMAC del suo ID con una chiave segreta del server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


# ------------------------------------------------------------------ archivio JSON
class JsonStore:
    """Dizionario salvato su file JSON, con scrittura atomica (niente file corrotti se si spegne)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        try:
            self.data: dict = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


# ------------------------------------------------------------------ password e TOTP
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, step_index: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", step_index), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return f"{code:0{digits}d}"


def check_totp(secret: str, code: str, last_step: int = -1, window: int = 1) -> int | None:
    """Ritorna lo step usato se il codice è valido (e non già usato), altrimenti None."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit():
        return None
    now = int(time.time() // 30)
    for s in range(now - window, now + window + 1):
        if s > last_step and hmac.compare_digest(totp(secret, s), code):
            return s
    return None


def otpauth_uri(secret: str, username: str, issuer: str = "Husky locale") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


# ------------------------------------------------------------------ limiti anti-spam
class RateLimiter:
    """Finestra scorrevole in memoria: allow(chiave, max, secondi)."""

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, limit: int, seconds: float) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def count(self, key: str, seconds: float) -> int:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - seconds:
                q.popleft()
            return len(q)

    def reset(self, key: str):
        with self.lock:
            self.hits.pop(key, None)


# ------------------------------------------------------------------ utenti e sessioni
class Auth:
    def __init__(self, cfg: dict, data_dir: Path):
        self.cfg = cfg
        self.users = JsonStore(data_dir / "users.json")
        self.sessions = JsonStore(data_dir / "sessions.json")
        self.devices = JsonStore(data_dir / "devices.json")
        key_path = data_dir / "secret.key"
        if not key_path.exists():
            key_path.write_text(secrets.token_hex(32), encoding="utf-8")
        self.secret = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
        self.limiter = RateLimiter()
        # codice per creare il primo admin: cambia a ogni avvio e si legge solo nel terminale del server
        self.setup_code = f"{secrets.randbelow(10 ** 8):08d}"
        self._purge_sessions()

    # ---------------------------------------------------------- utenti
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def needs_setup(self) -> bool:
        return self.enabled and not self.users.data

    def public_user(self, name: str) -> dict:
        u = self.users.data[name]
        return {"username": name, "role": u["role"], "totp": bool(u.get("totp_enabled")),
                "created": u.get("created"), "last_login": u.get("last_login")}

    def list_users(self) -> list[dict]:
        return [self.public_user(n) for n in sorted(self.users.data)]

    def validate_password(self, password: str) -> str | None:
        n = int(self.cfg.get("min_password_length", 8))
        if len(password or "") < n:
            return f"La password deve avere almeno {n} caratteri"
        return None

    def create_user(self, username: str, password: str, role: str = "user") -> str | None:
        if not USERNAME_RE.match(username or ""):
            return "Username: 3-32 caratteri tra lettere, numeri, punto, trattino, trattino basso"
        if role not in ("admin", "user"):
            return "Ruolo non valido"
        with self.users.lock:
            if username in self.users.data:
                return "Username già esistente"
            err = self.validate_password(password)
            if err:
                return err
            self.users.data[username] = {"password": hash_password(password), "role": role,
                                         "created": time.strftime("%Y-%m-%d %H:%M"), "totp_enabled": False}
            self.users.save()
        return None

    def delete_user(self, username: str) -> str | None:
        with self.users.lock:
            if username not in self.users.data:
                return "Utente inesistente"
            admins = [n for n, u in self.users.data.items() if u["role"] == "admin"]
            if self.users.data[username]["role"] == "admin" and len(admins) <= 1:
                return "Non puoi eliminare l'ultimo admin"
            del self.users.data[username]
            self.users.save()
        self.logout_user(username)
        return None

    def set_password(self, username: str, password: str) -> str | None:
        err = self.validate_password(password)
        if err:
            return err
        with self.users.lock:
            self.users.data[username]["password"] = hash_password(password)
            self.users.save()
        self.logout_user(username)
        return None

    def set_role(self, username: str, role: str) -> str | None:
        with self.users.lock:
            if role not in ("admin", "user") or username not in self.users.data:
                return "Dati non validi"
            admins = [n for n, u in self.users.data.items() if u["role"] == "admin"]
            if role == "user" and admins == [username]:
                return "Deve restare almeno un admin"
            self.users.data[username]["role"] = role
            self.users.save()
        return None

    # ---------------------------------------------------------- 2FA
    def totp_begin(self, username: str) -> str:
        secret = new_totp_secret()
        with self.users.lock:
            self.users.data[username]["totp_pending"] = secret
            self.users.save()
        return secret

    def totp_confirm(self, username: str, code: str) -> bool:
        with self.users.lock:
            u = self.users.data[username]
            secret = u.get("totp_pending")
            step = check_totp(secret, code) if secret else None
            if step is None:
                return False
            u.update(totp_secret=secret, totp_enabled=True, totp_last_step=step)
            u.pop("totp_pending", None)
            self.users.save()
        return True

    def totp_disable(self, username: str):
        with self.users.lock:
            u = self.users.data[username]
            for k in ("totp_secret", "totp_pending", "totp_last_step"):
                u.pop(k, None)
            u["totp_enabled"] = False
            self.users.save()

    # ---------------------------------------------------------- login
    def login(self, username: str, password: str, code: str, ip: str) -> tuple[str | None, str | None]:
        """Ritorna (token di sessione, errore). Blocca i tentativi ripetuti per IP e per utente."""
        lim = int(self.cfg.get("login_attempts", 5))
        window = 60 * int(self.cfg.get("lockout_minutes", 15))
        # per utente: pochi tentativi; per IP: un po' di più (più utenti possono condividere lo stesso IP)
        for key, n in ((f"loginfail:ip:{ip}", lim * 4), (f"loginfail:user:{username}", lim)):
            if self.limiter.count(key, window) >= n:
                return None, f"Troppi tentativi falliti: riprova tra {window // 60} minuti"
        u = self.users.data.get(username)
        ok = bool(u) and verify_password(password or "", u["password"])
        if ok and u.get("totp_enabled"):
            if not code:
                return None, "2fa"  # serve il codice: la pagina lo chiede
            with self.users.lock:
                step = check_totp(u["totp_secret"], code, u.get("totp_last_step", -1))
                if step is None:
                    ok = False
                else:
                    u["totp_last_step"] = step  # lo stesso codice non vale due volte
        if not ok:
            for key in (f"loginfail:ip:{ip}", f"loginfail:user:{username}"):
                self.limiter.allow(key, 10 ** 6, window)
            return None, "Credenziali non valide"
        self.limiter.reset(f"loginfail:user:{username}")
        token = secrets.token_urlsafe(32)
        days = float(self.cfg.get("session_days", 7))
        with self.sessions.lock:
            self.sessions.data[self._h(token)] = {"user": username, "ip": ip,
                                                  "expires": time.time() + days * 86400}
            self.sessions.save()
        with self.users.lock:
            u["last_login"] = time.strftime("%Y-%m-%d %H:%M")
            self.users.save()
        return token, None

    def user_from_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self.sessions.data.get(self._h(token))
        if not s or s["expires"] < time.time() or s["user"] not in self.users.data:
            return None
        u = self.users.data[s["user"]]
        must_2fa = bool(self.cfg.get("require_2fa")) and not u.get("totp_enabled")
        return {"username": s["user"], "role": u["role"], "must_setup_2fa": must_2fa}

    def logout(self, token: str):
        with self.sessions.lock:
            if self.sessions.data.pop(self._h(token or ""), None):
                self.sessions.save()

    def logout_user(self, username: str):
        with self.sessions.lock:
            for k in [k for k, s in self.sessions.data.items() if s["user"] == username]:
                del self.sessions.data[k]
            self.sessions.save()

    def _purge_sessions(self):
        with self.sessions.lock:
            now = time.time()
            for k in [k for k, s in self.sessions.data.items() if s["expires"] < now]:
                del self.sessions.data[k]
            self.sessions.save()

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    # ---------------------------------------------------------- dispositivi xiaozhi
    def device_token(self, device_id: str) -> str:
        return hmac.new(self.secret, device_id.lower().encode(), hashlib.sha256).hexdigest()

    def device_status(self, device_id: str) -> dict:
        """Per l'OTA: registra il dispositivo se nuovo (in attesa, con codice) e ritorna il suo stato."""
        did = (device_id or "").lower()[:64]
        with self.devices.lock:
            d = self.devices.data.get(did)
            if d is None:
                pending = [x for x in self.devices.data.values() if x["status"] == "pending"]
                if len(pending) >= int(self.cfg.get("max_pending_devices", 20)):
                    return {"status": "rejected"}
                d = self.devices.data[did] = {"status": "pending", "code": f"{secrets.randbelow(10 ** 6):06d}",
                                              "first_seen": time.strftime("%Y-%m-%d %H:%M"), "name": ""}
            d["last_seen"] = time.strftime("%Y-%m-%d %H:%M")
            self.devices.save()
            return dict(d)

    def approve_device(self, code: str = "", device_id: str = "", name: str = "") -> str | None:
        with self.devices.lock:
            for did, d in self.devices.data.items():
                if (device_id and did == device_id.lower()) or (code and d["status"] == "pending" and d["code"] == code.strip()):
                    d["status"] = "approved"
                    d["name"] = name or d.get("name") or did
                    d.pop("code", None)
                    self.devices.save()
                    return None
        return "Nessun dispositivo in attesa con questo codice"

    def remove_device(self, device_id: str):
        with self.devices.lock:
            self.devices.data.pop(device_id.lower(), None)
            self.devices.save()

    def device_ok(self, device_id: str, token: str) -> dict | None:
        d = self.devices.data.get((device_id or "").lower())
        if d and d["status"] == "approved" and hmac.compare_digest(self.device_token(device_id), token or ""):
            return d
        return None

    def list_devices(self) -> list[dict]:
        return [{"id": k, **{x: v.get(x) for x in ("status", "code", "name", "first_seen", "last_seen")}}
                for k, v in sorted(self.devices.data.items())]
