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

    # ------------------------------------------------ streaming: frasi pronte mentre il modello scrive
    from xiaozhi_locale.session import take_sentences
    ready, rest = take_sentences("Ciao! Oggi è una bella giornata. E dom")
    check("Streaming: le frasi complete partono subito, il resto aspetta",
          ready == ["Ciao! Oggi è una bella giornata."] and rest == "E dom", f"{ready} | {rest!r}")
    ready, rest = take_sentences("Ok. ")
    check("Streaming: frasi troppo corte aspettano la successiva", ready == [] and rest == "Ok. ", str(ready))
    long = "Il sistema solare è formato dal Sole e da otto pianeti, tra cui la Terra, Marte e Giove, che girano"
    ready, rest = take_sentences(long)
    check("Streaming: una frase lunghissima si spezza a una virgola", len(ready) == 1 and rest and ready[0].endswith(","),
          f"{ready} | {rest!r}")

    # ------------------------------------------------ argomenti degli strumenti controllati con lo schema
    from xiaozhi_locale.tools import validate_args
    sch = {"type": "object", "required": ["volume"], "properties": {
        "volume": {"type": "integer", "minimum": 0, "maximum": 100},
        "mode": {"type": "string", "enum": ["face", "object"]}}}
    check("Strumenti: '60' diventa 60", validate_args(sch, {"volume": "60"}) == ({"volume": 60}, None))
    check("Strumenti: argomento obbligatorio mancante rifiutato", validate_args(sch, {})[1] is not None)
    check("Strumenti: valore fuori dai limiti rifiutato", validate_args(sch, {"volume": 500})[1] is not None)
    check("Strumenti: valore non ammesso rifiutato", validate_args(sch, {"volume": 5, "mode": "rm -rf"})[1] is not None)
    called = []

    async def recorder(name, args):
        called.append(args)
        return "ok"

    vt = ToolSet()
    vt.add("set_volume", "", sch, recorder)
    res = asyncio.run(vt.call("set_volume", {"volume": 999}))
    check("Strumenti: con argomenti sbagliati lo strumento non viene chiamato", not called and res.startswith("Errore"), res)

    # ------------------------------------------------ registro eventi cifrato
    from xiaozhi_locale.events import EventLog
    ev_dir = T / "events"
    ev_dir.mkdir()
    ev = EventLog(ev_dir, Box(ev_dir / "k.key"))
    ev.add("login_failed", user="mario", ip="10.0.0.7")
    ev.add("device_approved", "Robot", "admin")
    raw = (ev_dir / "events.jsonl").read_text(encoding="utf-8")
    got_ev = ev.recent()
    check("Registro eventi: cifrato sul disco e leggibile dall'admin, dal più recente",
          "mario" not in raw and [e["kind"] for e in got_ev] == ["device_approved", "login_failed"]
          and got_ev[1]["ip"] == "10.0.0.7", str([e["kind"] for e in got_ev]))

    # ------------------------------------------------ strumenti personalizzati (comandi, web, MQTT)
    from xiaozhi_locale.custom_tools import PRESETS, CustomTools, ToolError
    ct_dir = T / "custom"
    ct_dir.mkdir()
    ct = CustomTools(ct_dir, Box(ct_dir / "k.key"))
    check("Strumenti personalizzati: tutti i tipi spenti all'inizio", not any(ct.kinds().values()))

    def rejected(d):
        try:
            ct.validate(d)
            return False
        except ToolError:
            return True

    base = {"name": "prova", "description": "prova", "roles": ["admin"]}
    check("Comandi: il programma non può dipendere dal modello",
          rejected({**base, "kind": "command", "argv": ["{x}"], "params": [{"name": "x"}]}))
    check("Comandi: {segnaposto} senza parametro rifiutato", rejected({**base, "kind": "command", "argv": ["echo", "{y}"]}))
    check("Web: il sito non può dipendere dal modello",
          rejected({**base, "kind": "web", "url": "https://{h}/x", "params": [{"name": "h"}]}))
    check("Web: solo http/https", rejected({**base, "kind": "web", "url": "file:///etc/passwd"}))
    for pr in PRESETS:
        ct.save({**pr, "id": None})
    check("Tutti gli strumenti già pronti sono validi", len(ct.store.data["tools"]) == len(PRESETS))
    check("Tipo spento: il modello non vede nessuno strumento", len(ct.toolset_for("admin")) == 0)
    ct.set_kind("command", True)
    adm, usr = ct.toolset_for("admin").names(), ct.toolset_for("user").names()
    check("Tipo acceso: l'admin vede i comandi, gli utenti no (ruoli)",
          "spazio_disco" in adm and "spazio_disco" not in usr and "meteo_oggi" not in adm, f"{adm} | {usr}")
    disk = asyncio.run(ct.toolset_for("admin").call("spazio_disco", {}))
    check("Comando già pronto eseguito (spazio su disco)", "Spazio libero" in disk, disk)
    ping = next(t for t in ct.store.data["tools"] if t["name"] == "ping_dispositivo")
    bad = asyncio.run(ct.run(ping, {"indirizzo": "127.0.0.1; rm -rf /"}))
    check("Comandi: valore con caratteri pericolosi rifiutato", bad.startswith("Errore"), bad)
    ct.set_kind("command", False)
    gone = asyncio.run(ct._caller(ping["id"])("ping_dispositivo", {"indirizzo": "127.0.0.1"}))
    check("Comandi: spegnendo il tipo lo strumento smette subito di funzionare", "disattivato" in gone, gone)
    sec = ct.save({**base, "name": "api_test", "kind": "web", "url": "http://127.0.0.1:1/x",
                   "headers": {"Authorization": "Bearer SEGRETO-123"}})
    raw = (ct_dir / "custom_tools.json").read_text(encoding="utf-8")
    pub = next(t for t in ct.public() if t["id"] == sec["id"])
    check("Web: chiavi API cifrate sul disco e mai rimandate al browser",
          "SEGRETO-123" not in raw and pub["headers"]["Authorization"] == "••••••")
    ct.save({**sec, "headers": {"Authorization": "••••••"}})
    kept = ct._find(sec["id"])["headers"]["Authorization"]
    check("Web: salvando di nuovo senza cambiarla, la chiave resta", ct.box.decrypt(kept) == "Bearer SEGRETO-123")
    sensor = next(t for t in ct.store.data["tools"] if t["kind"] == "mqtt")
    check("MQTT spento: risposta chiara invece di un errore",
          "MQTT" in asyncio.run(ct.run(sensor, {})))

    # ------------------------------------------------ admin predefinito con cambio password obbligatorio
    dd = T / "default_admin"
    dd.mkdir()
    da = Auth({"enabled": True}, dd)
    created = da.ensure_default_admin()
    tok, _ = da.login("admin", "Passw0rd!", "", "127.0.0.1")
    ident = da.user_from_token(tok)
    check("Primo avvio: admin / Passw0rd! creato, ma deve cambiare la password",
          created and ident and ident["must_change_password"] and not da.ensure_default_admin())
    check("La nuova password non può essere di nuovo quella predefinita", da.set_password("admin", "Passw0rd!") is not None)
    da.set_password("admin", "Nuova-Password-1")
    tok, _ = da.login("admin", "Nuova-Password-1", "", "127.0.0.1")
    check("Dopo il cambio l'admin può usare tutto", da.user_from_token(tok)["must_change_password"] is False)
    off = T / "default_off"
    off.mkdir()
    check("Con auth.default_admin: false nessun account predefinito (si usa il codice del terminale)",
          not Auth({"enabled": True, "default_admin": False}, off).ensure_default_admin())

    # ------------------------------------------------ gruppi di strumenti scelti dal modello
    from xiaozhi_locale.tools import ToolGroups
    g1, g2, always = ToolSet(), ToolSet(), ToolSet()
    g1.add("set_volume", "volume", {"type": "object", "properties": {}}, noop)
    g2.add("get_result", "telecamera", {"type": "object", "properties": {}}, noop)
    always.add("ora_attuale", "ora", {"type": "object", "properties": {}}, noop)
    opened = set()
    tg = ToolGroups({"dispositivo": ("volume", g1), "telecamera": ("cosa vede", g2), "vuoto": ("niente", ToolSet())},
                    always, opened)
    names = lambda: [s["function"]["name"] for s in tg.specs()]  # noqa: E731
    check("Gruppi: all'inizio il modello vede solo l'elenco dei gruppi (e gli strumenti sempre attivi)",
          names() == ["ora_attuale", "apri_strumenti"], str(names()))
    res = asyncio.run(tg.call("apri_strumenti", {"gruppo": "telecamera"}))
    check("Gruppi: aperto un gruppo, il modello vede i suoi strumenti e gli si dice di usarli",
          "get_result" in names() and "set_volume" not in names() and "non inventare" in res and opened == {"telecamera"})
    asyncio.run(tg.call("apri_strumenti", {"gruppo": "dispositivo"}))
    check("Gruppi: aperti tutti, sparisce apri_strumenti; i gruppi vuoti non compaiono",
          "apri_strumenti" not in names() and "vuoto" not in tg.groups)

    # ------------------------------------------------ promemoria per utente
    from xiaozhi_locale.notes import Notes
    nd = T / "notes"
    nd.mkdir()
    notes = Notes(nd, Box(nd / "k.key"))
    nts = notes.toolset("marco")
    asyncio.run(nts.call("promemoria_aggiungi", {"testo": "portare il libro di storia"}))
    asyncio.run(nts.call("promemoria_aggiungi", {"testo": "consegnare la ricerca"}))
    other = asyncio.run(notes.toolset("anna").call("promemoria_elenco", {}))
    lst = asyncio.run(nts.call("promemoria_elenco", {}))
    gone = asyncio.run(nts.call("promemoria_rimuovi", {"numero_o_testo": "libro"}))
    raw_notes = (nd / "notes.json").read_text(encoding="utf-8")
    check("Promemoria: per utente, cifrati, si aggiungono e si tolgono",
          "storia" in lst and "Non ci sono" in other and "storia" in gone and "ricerca" not in raw_notes
          and "storia" not in asyncio.run(nts.call("promemoria_elenco", {})))

    # ------------------------------------------------ risposte immediate
    from xiaozhi_locale.faq import FAQ
    fd = T / "faq"
    fd.mkdir()
    faq = FAQ(fd)
    faq.save({"questions": ["A che ora inizia la lezione?"], "answer": "Alle 8 e 10."})
    faq.save({"questions": ["Quando è la verifica di matematica?"], "answer": "Giovedì."})
    check("Risposte immediate: domanda uguale o con piccoli errori di trascrizione",
          faq.match("a che ora inizia la lezione") and faq.match("quand'è la verifica di matematica")
          and faq.match("quand'è la verifica di matematica")[0] == "Giovedì.")
    check("Risposte immediate: domande diverse NON confuse (finisce/inizia, storia/matematica)",
          faq.match("a che ora finisce la lezione") is None and faq.match("quando è la verifica di storia") is None
          and faq.match("cosa vedi con la telecamera") is None)

    # ------------------------------------------------ feed RSS (notizie, allerte)
    from xiaozhi_locale.custom_tools import _rss_titles
    feed = ("<rss><channel><title>Feed</title><item><title><![CDATA[Allerta gialla temporali - Lombardia]]></title></item>"
            "<item><title>Allerta nebbia - Marche &amp; Umbria</title></item></channel></rss>")
    check("Feed RSS: titoli letti e filtrati per regione",
          "Lombardia" in _rss_titles(feed) and "Marche & Umbria" in _rss_titles(feed)
          and "Marche" not in _rss_titles(feed, "lombardia") and "Nessuna" in _rss_titles(feed, "Sicilia"))

    # ------------------------------------------------ statistiche delle prestazioni
    from collections import deque
    from xiaozhi_locale.app import perf_summary
    st = deque([{"time": 0, "voice": True, "asr": 0.5, "llm_first": 0.4, "first_audio": 1.5},
                {"time": 0, "voice": False, "asr": None, "llm_first": 0.3, "first_audio": 0.9}])
    ps = perf_summary(st)
    check("Statistiche: tempi medi per voce e testo",
          ps["turns"] == 2 and ps["first_audio_voice"]["avg"] == 1.5 and ps["first_audio_text"]["avg"] == 0.9, str(ps))

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

    # ------------------------------------------------ MCP: tipo di collegamento, programmi locali nella privacy
    from xiaozhi_locale.tools import mcp_transport
    got = [mcp_transport("http://192.168.1.9:3000/sse"), mcp_transport("http://192.168.1.9:8000/mcp"),
           mcp_transport("", "npx"), mcp_transport("http://x/sse", transport="http")]
    check("MCP: SSE, Streamable HTTP o programma locale riconosciuti", got == ["sse", "http", "stdio", "http"], str(got))
    pv = privacy.check({"llm": {"base_url": "http://127.0.0.1:11434/v1"},
                        "mcp_servers": [{"name": "fs", "command": "npx"}, {"name": "cam", "url": "http://8.8.8.8/sse"}]})
    check("Privacy: programma MCP locale ok, server MCP su Internet segnalato",
          [x["local"] for x in pv] == [True, True, False], str([(x["what"], x["local"]) for x in pv]))

    # ------------------------------------------------ ESPHome: WAV per le schede con media player
    import wave as _wave

    import numpy as np
    from xiaozhi_locale.esphome_native import make_wav
    wav = make_wav(np.zeros(22050, np.int16), 22050)
    with _wave.open(__import__("io").BytesIO(wav)) as w:
        check("ESPHome: risposta in WAV mono 16 bit", w.getnchannels() == 1 and w.getsampwidth() == 2
              and w.getnframes() == 22050)

    # ------------------------------------------------ Fase D: documenti, lezioni, memoria, personaggi
    from xiaozhi_locale import knowledge as kn_mod
    from xiaozhi_locale.lessons import is_stop
    from xiaozhi_locale.memory import PERSONAL_RE
    from xiaozhi_locale.personas import Personas
    check("Documenti: parole simili hanno la stessa radice, parole vuote tolte",
          kn_mod.tokens("I regolamenti del laboratorio")[:2] == kn_mod.tokens("regolamento laboratori")[:2]
          and "del" not in kn_mod.tokens("del"))
    parts = kn_mod.chunk_text("Frase di prova numero uno. " * 200)
    check("Documenti: testo diviso in pezzi che finiscono a fine frase",
          len(parts) > 3 and all(p.endswith(".") for p in parts[:-1]) and max(map(len, parts)) <= kn_mod.CHUNK)
    k = kn_mod.Knowledge(T / "kn")
    k.add("a.txt", ("Il bidello si chiama Mario e apre la palestra alle otto. " * 3).encode(), "Scuola", ["admin"])
    check("Documenti: un documento solo per admin non compare agli utenti",
          k.search("chi apre la palestra", "admin") and not k.search("chi apre la palestra", "user")
          and not k.toolset("user").names())
    check("Documenti: contesto automatico solo se la domanda è pertinente",
          "Mario" in k.context_for("Chi apre la palestra la mattina?", "admin")
          and k.context_for("Quanto fa 7 per 8?", "admin") == "")
    check("Lezione: \"fine della lezione\" riconosciuto, frasi normali no",
          is_stop("Bene ragazzi, fine della lezione.") and is_stop("la lezione è finita") and not is_stop("la lezione di oggi"))
    check("Memoria: si estrae solo da frasi personali",
          bool(PERSONAL_RE.search("Mi chiamo Ada")) and not PERSONAL_RE.search("Quanto fa 7 per 8?"))
    from xiaozhi_locale.engines import HALLUCINATION_RE, _foreign_ratio
    check("Riconoscimento vocale: frasi inventate sul rumore riconosciute, l'italiano con gli accenti no",
          _foreign_ratio("Hãy subscribe cho kênh La La School Để không bỏ lỡ những video") > 0.05
          and _foreign_ratio("Perché è così? Però sì, la città è più bella") == 0
          and HALLUCINATION_RE.search("Sottotitoli creati dalla comunità Amara.org")
          and not HALLUCINATION_RE.search("Quante zampe ha un gatto?"))
    (T / "ps").mkdir()
    ps = Personas(T / "ps")
    ps.save({"name": "Tutor di inglese", "prompt": "Sei un tutor di inglese.", "language": "en"}, [])
    check("Personaggi: trovati anche da un nome detto a voce", (ps.find("il tutor d'inglese") or {}).get("name") == "Tutor di inglese")

    shutil.rmtree(T, ignore_errors=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test dei componenti superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
