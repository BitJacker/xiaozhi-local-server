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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale import privacy  # noqa: E402
from xiaozhi_locale.cryptobox import Box  # noqa: E402
from xiaozhi_locale.history import History  # noqa: E402
from xiaozhi_locale.tools import ToolSet, content_to_text  # noqa: E402

T = ROOT / "tests" / "_units_test"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


async def noop(name, args):
    return "ok"


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
