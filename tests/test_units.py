"""Test dei singoli componenti (senza server): cronologia, cifratura, permessi, immagini, privacy."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale import privacy  # noqa: E402
from xiaozhi_locale.app import security  # noqa: E402
from xiaozhi_locale.auth import Auth  # noqa: E402
from xiaozhi_locale.cryptobox import Box  # noqa: E402
from xiaozhi_locale.history import History  # noqa: E402
from xiaozhi_locale.session import Session  # noqa: E402
from xiaozhi_locale.tools import ToolSet, content_to_text  # noqa: E402

T = ROOT / "tests" / "_units_test"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


async def noop(name, args):
    return "ok"


async def audio_limit_check() -> bool:
    """Il limite della frase vale anche nel push-to-talk manuale."""
    app = {"cfg": {"vad": {"silence_ms": 800, "min_speech_ms": 300,
                              "energy_threshold": 0.012, "max_utterance_s": 1}}, "describe": None}
    s = Session(app, None, SimpleNamespace(headers={}, query={}, remote="127.0.0.1"), user="audit")
    s.pcm_mode, s.listen_mode = True, "manual"
    s.start_listening()
    s.on_audio(bytes(16000 * 2 * 2))  # due secondi PCM, con un limite di un secondo
    stopped = not s.listening and s.buffer_samples == 0
    if s.turn_task:
        s.turn_task.cancel()
        await asyncio.gather(s.turn_task, return_exceptions=True)
    return stopped


async def http_api_requires_https() -> bool:
    """Le API del browser non devono accettare POST HTTP da un altro computer in LAN."""
    req = SimpleNamespace(
        app={"cfg": {"limits": {}}, "https_url": "https://192.168.1.9:8443"},
        path="/api/login", method="POST", secure=False, remote="192.168.1.20",
        headers={"X-Requested-With": "husky"}, path_qs="/api/login", cookies={},
    )

    async def handler(_):
        return web.json_response({"ok": True})

    resp = await security(req, handler)
    return resp.status == 426


def main():
    shutil.rmtree(T, ignore_errors=True)
    T.mkdir(parents=True)

    # ------------------------------------------------ cronologia: scadenza a server acceso + cifratura
    box = Box(T / "encryption.key")
    h = History({"enabled": True, "keep_days": 1}, T, box)
    old_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
    (T / "history" / f"{old_day}.jsonl").write_text('{"text": "vecchia"}\n', encoding="utf-8")
    h.append("utente1", "web", "s1", "user", "domanda segreta di oggi")
    check("Cronologia: il file vecchio c'è finché non passa la pulizia", old_day in h.days())
    h.cleanup()  # quello che fa ogni ora il server acceso
    check("Cronologia: scaduta cancellata anche a server acceso", old_day not in h.days(), str(h.days()))
    raw = (T / "history" / f"{time.strftime('%Y-%m-%d')}.jsonl").read_text(encoding="utf-8")
    check("Cronologia cifrata sul disco (il testo non si legge)", "segreta" not in raw and raw.startswith("enc1:"))
    msgs = h.read(time.strftime("%Y-%m-%d"))
    check("Cronologia cifrata: la pagina la legge normalmente", msgs and msgs[0]["text"] == "domanda segreta di oggi")
    (T / "history" / f"{time.strftime('%Y-%m-%d')}.jsonl").open("a", encoding="utf-8").write(
        json.dumps({"text": "riga in chiaro di una versione precedente"}) + "\n")
    check("Cronologia: righe in chiaro delle versioni precedenti ancora leggibili",
          len(h.read(time.strftime("%Y-%m-%d"))) == 2)
    other = Box(T / "altra.key")
    try:
        other.decrypt(raw.strip())
        check("Senza la chiave giusta la cronologia non si decifra", False)
    except Exception:
        check("Senza la chiave giusta la cronologia non si decifra", True)
    check("Default: cronologia conservata 30 giorni", History({}, T / "x", None).keep_days == 30)

    # ------------------------------------------------ permessi strumenti
    ts = ToolSet()
    for n in ("get_recognition_result", "manage_applications", "learn_object", "forget_learned_ids",
              "take_photo", "set_model_parameters", "self.screen.get_brightness"):
        ts.add(n, n, None, noop, prefix="huskylens_" if not n.startswith("self.") else "")
    import yaml
    user = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["tool_permissions"]["user"]
    allowed = ts.filtered(user).names()
    check("Permessi: l'utente può leggere i riconoscimenti e scattare foto",
          "huskylens_get_recognition_result" in allowed and "huskylens_take_photo" in allowed
          and "self_screen_get_brightness" in allowed, str(allowed))
    check("Permessi: l'utente NON può far imparare, dimenticare o cambiare parametri",
          not any(x in allowed for x in ("huskylens_learn_object", "huskylens_forget_learned_ids",
                                         "huskylens_set_model_parameters")), str(allowed))
    check("Permessi: l'admin ha tutto", len(ts.filtered(["*"])) == len(ts))

    # ------------------------------------------------ limiti audio, revoca sessioni e onboarding dispositivi
    check("Push-to-talk: il limite massimo di audio vale anche senza 'listen stop'", asyncio.run(audio_limit_check()))
    auth_dir = T / "auth"
    auth_dir.mkdir()
    auth = Auth({"enabled": True, "max_pending_devices": 20, "max_pending_per_ip": 2,
                 "pending_device_minutes": 10}, auth_dir)
    auth.create_user("alice", "Password-Alice-1", "admin")
    auth.create_user("bruno", "Password-Bruno-1", "admin")
    token, err = auth.login("alice", "Password-Alice-1", "", "127.0.0.1")
    ident = auth.user_from_token(token)
    live = Session({"cfg": {"vad": {}}, "describe": None}, None,
                   SimpleNamespace(headers={}, query={}, remote="127.0.0.1"), user=ident["username"],
                   role=ident["role"], identity_check=lambda: auth.user_from_token(token))
    auth.delete_user("alice")
    check("Revoca: una sessione WebSocket aperta non resta autorizzata", not live.refresh_identity())
    states = [auth.device_status(f"audit-{n}", f"client-{n}", "10.0.0.8")["status"] for n in range(3)]
    check("Onboarding: un IP non può riempire tutti gli slot in attesa", states == ["pending", "pending", "rejected"], str(states))
    auth.devices.data["audit-0|client-0"]["created_at"] = 0
    auth.devices.save()
    check("Onboarding: richieste scadute liberano lo slot", auth.device_status("audit-2", "client-2", "10.0.0.8")["status"] == "pending")
    check("API browser: un POST HTTP dalla LAN è rifiutato quando HTTPS è attivo", asyncio.run(http_api_requires_https()))

    got = {}

    async def setter(name, args):
        got.update(args)
        return "ok"
    t2 = ToolSet()
    t2.add("self.screen.set_brightness", "luminosità",
           {"type": "object", "properties": {"brightness": {"type": "integer"}}}, setter)
    asyncio.run(t2.call("self_screen_set_brightness", {"brightness": 50, "function": "inventato"}))
    check("Argomenti inventati dal modello scartati prima di chiamare lo strumento", got == {"brightness": 50}, str(got))

    # ------------------------------------------------ immagini restituite dagli strumenti MCP
    png = base64.b64encode(b"\x89PNG finta").decode()
    content = [{"type": "text", "text": "foto scattata"}, {"type": "image", "data": png, "mimeType": "image/png"}]

    async def describe(img, q):
        return f"una persona davanti alla camera ({len(img)} byte)"
    out = asyncio.run(content_to_text(content, describe, "take_photo"))
    check("Immagine MCP descritta dal modello visivo", "una persona davanti alla camera" in out, out[:90])
    out = asyncio.run(content_to_text(content, None, "take_photo"))
    check("Senza modello visivo: avviso chiaro invece del segnaposto", "llm.vision_model" in out)

    # ------------------------------------------------ privacy: indirizzi locali o esterni
    cases = {"127.0.0.1": True, "localhost": True, "192.168.1.50": True, "10.0.0.5": True, "172.20.1.1": True,
             "8.8.8.8": False, "api.openai.com": False, "": False}
    wrong = [h for h, exp in cases.items() if privacy.host_is_local(h) != exp]
    check("Privacy: indirizzi locali ed esterni riconosciuti", not wrong, str(wrong))
    cfgdir = T / "srv"
    cfgdir.mkdir()
    base = (ROOT / "config.yaml").read_text(encoding="utf-8")
    bad = base.replace("base_url: http://127.0.0.1:11434/v1", "base_url: https://api.openai.com/v1") \
              .replace("data_dir: data", f"data_dir: {(cfgdir / 'data').as_posix()}")
    (cfgdir / "cfg.yaml").write_text(bad, encoding="utf-8")
    res = subprocess.run([sys.executable, "server.py", "-c", str(cfgdir / "cfg.yaml"), "--selftest"], cwd=ROOT,
                         capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    check("Privacy: con un LLM su Internet il server si rifiuta di partire",
          res.returncode == 3 and "NON parte" in res.stdout, f"exit={res.returncode}")
    ok = bad.replace("allow_external: false", "allow_external: true")
    (cfgdir / "cfg.yaml").write_text(ok, encoding="utf-8")
    res = subprocess.run([sys.executable, "server.py", "-c", str(cfgdir / "cfg.yaml"), "--selftest"], cwd=ROOT,
                         capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    check("Privacy: parte solo se lo permetti esplicitamente (allow_external)", "SELFTEST OK" in res.stdout,
          f"exit={res.returncode}")

    shutil.rmtree(T, ignore_errors=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test dei componenti superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
