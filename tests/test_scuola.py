"""Test della Fase D (funzioni per la classe): documenti del prof, memoria per utente, personaggi, modalità lezione.

Con il modello vero le domande sono normali ("qual è la parola d'ordine dell'armadio?"); con --mock si usa la frase
"usa lo strumento ..." che il modello finto riconosce. In entrambi i casi si controllano anche permessi, cifratura
sul disco e pagine/API.  Uso:  python tests/test_scuola.py [--mock]
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from run_all import MOCK, PY, env, make_config, stop, wait_health  # noqa: E402

from xiaozhi_locale.auth import Auth  # noqa: E402

PORT = 8100
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "tests" / "_data_scuola"
H = {"X-Requested-With": "husky"}
RESULTS = []
SECRET = "GIRASOLE"
DOC = (f"Regolamento del laboratorio di scienze.\n\nArticolo 1. Si entra in laboratorio solo con il camice e gli "
       f"occhiali di protezione. Le borse restano fuori, sugli appendiabiti.\n\nArticolo 2. L'armadio dei reagenti è "
       f"chiuso a chiave: la parola d'ordine per chiedere la chiave al tecnico è {SECRET}.\n\nArticolo 3. Se si rompe "
       f"una provetta non si raccolgono i vetri a mano: si avvisa subito il professore, che usa la scopa e la paletta "
       f"del banco 4.\n\n" + "Testo di riempimento sulla sicurezza in laboratorio. " * 60)


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


def make_pdf(text: str) -> bytes:
    """Un PDF minimo con del testo (per provare la lettura dei PDF senza file esterni)."""
    content = "BT /F1 12 Tf 50 750 Td (" + text.replace("(", "").replace(")", "") + ") Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


async def login(user, pw):
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    async with s.post(BASE + "/api/login", json={"username": user, "password": pw}, headers=H) as r:
        assert r.status == 200, await r.text()
    return s


class Chat:
    """Una conversazione dalla pagina dell'assistente (WebSocket del client web)."""

    def __init__(self, s):
        self.s, self.msgs = s, []

    async def __aenter__(self):
        self.ws = await self.s.ws_connect(BASE.replace("http", "ws") + "/xiaozhi/v1/")
        await self.ws.send_str(json.dumps({"type": "hello", "audio_params": {"format": "pcm", "sample_rate": 16000}}))
        self.task = asyncio.create_task(self._read())
        await self.wait(lambda m: m.get("type") == "hello")
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        await self.ws.close()

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.msgs.append(json.loads(msg.data))

    async def send(self, obj):
        await self.ws.send_str(json.dumps(obj))

    async def wait(self, pred, timeout=120, start=0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for m in self.msgs[start:]:
                if pred(m):
                    return m
            await asyncio.sleep(0.1)
        return None

    async def ask(self, text: str) -> str:
        n = len(self.msgs)
        await self.send({"type": "text", "text": text})
        await self.wait(lambda m: m.get("type") == "tts" and m.get("state") == "stop", start=n)
        return " ".join(m.get("text", "") for m in self.msgs[n:]
                        if m.get("type") == "tts" and m.get("state") == "sentence_start")


async def ask_once(s, text):
    async with Chat(s) as c:
        return await c.ask(text)


async def main_async():
    prof = await login("prof", "Password-Prof-123")
    stud = await login("studente", "Password-Stud-123")
    try:
        # ================= documenti (base di conoscenza)
        async def upload(sess, name, data, title="", roles="admin,user,device"):
            f = aiohttp.FormData()
            f.add_field("title", title)
            f.add_field("roles", roles)
            f.add_field("file", data, filename=name)
            async with sess.post(BASE + "/api/admin/knowledge-upload", data=f, headers=H) as r:
                return r.status, await r.json()
        st, res = await upload(prof, "regolamento.txt", DOC.encode("utf-8"), "Regolamento di laboratorio")
        doc = res.get("doc", {})
        check("Documento di testo caricato e diviso in parti", st == 200 and doc.get("chunks", 0) >= 3, str(doc)[:120])
        st2, res2 = await upload(prof, "circolare.pdf", make_pdf("Circolare: la gita a Firenze e il 12 maggio, partenza alle 7."),
                                 "Circolare gita", "admin")
        check("PDF letto (testo estratto)", st2 == 200 and res2.get("doc", {}).get("chunks") == 1, str(res2)[:100])
        st3, res3 = await upload(prof, "virus.exe", b"MZ" * 100)
        check("Formato non supportato rifiutato", st3 == 400, res3.get("error", ""))
        st4, _ = await upload(stud, "x.txt", b"ciao" * 50)
        check("Uno studente non può caricare documenti", st4 == 403)
        async with prof.get(BASE + "/api/admin/knowledge?q=parola d'ordine armadio reagenti") as r:
            hits = (await r.json())["results"]
        check("Ricerca: il pezzo giusto è il primo", hits and SECRET in hits[0]["text"], str(hits[:1])[:120])
        raw = b"".join(p.read_bytes() for p in (DATA / "knowledge").glob("*.json"))
        check("Testo dei documenti cifrato sul disco", raw and SECRET.encode() not in raw and b"Firenze" not in raw)

        q = ("Usa lo strumento cerca_documenti con domanda=armadio" if MOCK
             else "Qual è la parola d'ordine per chiedere la chiave dell'armadio dei reagenti in laboratorio?")
        a = await ask_once(prof, q)
        check("Domanda sul regolamento: risposta presa dal documento", SECRET.lower() in a.lower(), a[:100])
        q = ("Usa lo strumento cerca_documenti con domanda=gita" if MOCK
             else "Quando è la gita a Firenze? Cerca nei documenti.")
        a = await ask_once(stud, q)
        check("Studente: il documento riservato agli admin non viene usato", "12 maggio" not in a, a[:100])

        # ================= memoria per utente
        q = ("Usa lo strumento memoria_salva con fatto=Ada_prepara_esame_biologia" if MOCK
             else "Ricordati che mi chiamo Ada e che sto preparando l'esame di biologia.")
        await ask_once(stud, q)
        for _ in range(40):  # col modello vero il ricordo può arrivare dall'estrazione in sottofondo, dopo la risposta
            async with stud.get(BASE + "/api/memory") as r:
                mem = await r.json()
            # col modello vero si aspetta anche il nome: il modello può salvare prima solo una parte
            if mem["facts"] and (MOCK or any("chiam" in f["text"].lower() for f in mem["facts"])):
                break
            await asyncio.sleep(0.5)
        check("Memoria: il ricordo è salvato per lo studente", mem["enabled"] and any("Ada" in f["text"] for f in mem["facts"]),
              str(mem["facts"])[:120])
        if not MOCK:
            a = await ask_once(stud, "Come mi chiamo?")
            check("Memoria: nella conversazione dopo Husky si ricorda il nome", "ada" in a.lower(), a[:80])
        async with prof.get(BASE + "/api/memory") as r:
            pm = await r.json()
        check("Memoria: ognuno vede solo i propri ricordi", not any("Ada" in f["text"] for f in pm["facts"]))
        raw = (DATA / "memory.json").read_text(encoding="utf-8")
        check("Memoria cifrata sul disco", "Ada" not in raw and "biologia" not in raw)
        async with stud.delete(BASE + "/api/memory", json={}, headers=H) as r:
            gone = (await r.json())["facts"]
        check("Memoria: lo studente cancella tutto dalla pagina Account", gone == [])

        # ================= personaggi
        async def pe(data=None):
            if data is None:
                async with prof.get(BASE + "/api/admin/personas") as r:
                    return r.status, await r.json()
            async with prof.post(BASE + "/api/admin/personas", json=data, headers=H) as r:
                return r.status, await r.json()
        st, res = await pe({"action": "save", "name": "Tutor di inglese", "language": "en", "voice": "nessuna.onnx",
                            "prompt": "You are an English tutor."})
        check("Personaggio con una voce inesistente: rifiutato", st == 400, res.get("error", ""))
        st, res = await pe({"action": "save", "name": "Tutor di inglese", "language": "en", "speed": 1.0,
                            "prompt": "You are Tutor, an English tutor for 14-year-old Italian students. Always answer in "
                                      "simple English, in at most 2 short sentences. Never use Italian."})
        tutor = next((p for p in res.get("items", []) if p["name"] == "Tutor di inglese"), {})
        check("Personaggio creato", st == 200 and tutor.get("language") == "en")
        async with stud.get(BASE + "/api/personas") as r:
            pub = await r.json()
        check("La pagina dell'assistente vede i personaggi (senza le istruzioni)",
              pub["items"] and "prompt" not in pub["items"][0], str(pub)[:100])
        async with Chat(stud) as c:
            await c.send({"type": "persona", "id": tutor["id"]})
            m = await c.wait(lambda x: x.get("type") == "persona", 10)
            check("Scelta del personaggio dalla pagina dell'assistente", m and m.get("name") == "Tutor di inglese")
            if not MOCK:
                a = await c.ask("Qual è la capitale della Francia?")
                check("Il personaggio risponde con le sue istruzioni (in inglese)",
                      "paris" in a.lower() and " the " in f" {a.lower()} ", a[:100])
        async with Chat(stud) as c:
            q = "Usa lo strumento cambia_assistente con nome=inglese" if MOCK else "Passa al tutor di inglese, per favore."
            await c.ask(q)
            if MOCK:
                said = " ".join(x.get("text", "") for x in c.msgs if x.get("type") == "tts")
                check("Cambio di personaggio a voce", "Tutor di inglese" in said, said[:100])
            else:
                a = await c.ask("Qual è la capitale della Germania?")
                check("Cambio di personaggio a voce: poi risponde il tutor (in inglese)",
                      "berlin" in a.lower() and "berlino" not in a.lower(), a[:100])
        st, res = await pe({"action": "assign", "device": "aa:bb:cc:dd:ee:ff", "id": tutor["id"]})
        check("Personaggio assegnato a un dispositivo", st == 200 and res["assigned"].get("aa:bb:cc:dd:ee:ff") == tutor["id"])

        # ================= modalità lezione
        async with Chat(stud) as c:
            await c.send({"type": "lesson", "state": "start"})
            m = await c.wait(lambda x: x.get("type") == "lesson", 10)
            check("Uno studente non può avviare la modalità lezione", m and m.get("state") == "denied")
        async with Chat(prof) as c:
            await c.send({"type": "lesson", "state": "start", "title": "Fotosintesi"})
            m = await c.wait(lambda x: x.get("type") == "lesson" and x.get("state") == "started", 10)
            lines = ["Oggi parliamo della fotosintesi clorofilliana, il processo con cui le piante producono il loro cibo.",
                     "Le piante usano la luce del sole, l'acqua e l'anidride carbonica per produrre glucosio e ossigeno.",
                     "La fotosintesi avviene nei cloroplasti, che contengono la clorofilla, il pigmento verde.",
                     "Per giovedì fate gli esercizi a pagina 12 del libro di scienze."]
            n = len(c.msgs)
            for ln in lines:
                await c.send({"type": "text", "text": ln})
                await c.wait(lambda x, ln=ln: x.get("type") == "lesson" and x.get("text") == ln, 20, n)
            answered = [x for x in c.msgs[n:] if x.get("type") == "tts"]
            check("Modalità lezione: trascrive senza rispondere", m and not answered)
            await c.send({"type": "text", "text": "Bene ragazzi, fine della lezione."})
            done = await c.wait(lambda x: x.get("type") == "lesson" and x.get("state") == "done", 240)
            summary = (done or {}).get("summary", "")
            check("Fine della lezione detta a voce: riassunto pronto", done and "Argomento" in summary
                  and ("12" in summary or "esercizi" in summary.lower()), summary[:160].replace("\n", " | "))
        async with prof.get(BASE + "/api/lessons") as r:
            items = (await r.json())["items"]
        lid = items[0]["id"] if items else ""
        async with prof.get(BASE + f"/api/lessons?id={lid}") as r:
            det = await r.json()
        async with prof.get(BASE + f"/api/lessons?id={lid}&format=txt") as r:
            txt = await r.text()
        check("Pagina Lezioni: trascrizione e riassunto, anche in .txt",
              len(det.get("transcript", [])) == 4 and "fotosintesi" in txt and "RIASSUNTO" in txt)
        async with stud.get(BASE + f"/api/lessons?id={lid}") as r:
            check("Uno studente non vede la lezione del prof", r.status == 404)
        raw = b"".join(p.read_bytes() for p in (DATA / "lessons").glob("*.json"))
        check("Lezioni cifrate sul disco", raw and b"fotosintesi" not in raw)
        async with Chat(prof) as c:
            q = "Usa lo strumento lezione_inizia" if MOCK else "Inizia la modalità lezione, per favore."
            await c.ask(q)
            st = await c.wait(lambda x: x.get("type") == "lesson" and x.get("state") == "started", 5)
            check("Modalità lezione avviata a voce (strumento del modello)", st is not None)
            await c.send({"type": "text", "text": "Questa è una frase della seconda lezione, che si interrompe."})
            await c.wait(lambda x: x.get("type") == "lesson" and x.get("state") == "text", 20)
        ok = False
        for _ in range(120):  # connessione chiusa durante la lezione: salvata comunque
            async with prof.get(BASE + "/api/lessons") as r:
                if len((await r.json())["items"]) == 2:
                    ok = True
                    break
            await asyncio.sleep(1)
        check("Connessione chiusa a metà lezione: la lezione viene salvata", ok)
        async with prof.get(BASE + "/lezioni") as r:
            check("Pagina /lezioni disponibile", r.status == 200 and "lezioni" in (await r.text()).lower())
        async with prof.get(BASE + "/api/admin/events") as r:
            kinds = {e["kind"] for e in (await r.json())["events"]}
        check("Registro: documenti, personaggi e lezioni registrati", {"knowledge", "persona", "lesson"} <= kinds, str(kinds))
    finally:
        await prof.close()
        await stud.close()


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    a = Auth({"enabled": True}, DATA)
    a.create_user("prof", "Password-Prof-123", "admin")
    a.create_user("studente", "Password-Stud-123", "user")
    cfg = make_config(PORT, "tests/_data_scuola", auth=True, turns=200, huskylens=False)
    (ROOT / "tests" / "config.scuola.yaml").write_text(cfg, encoding="utf-8")
    procs = []
    if MOCK and not _port_open(1299):
        procs.append(subprocess.Popen([PY, "tests/mock_llm.py"], cwd=ROOT, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    log = open(DATA / "server.log", "w", encoding="utf-8")
    procs.append(subprocess.Popen([PY, "server.py", "-c", "tests/config.scuola.yaml"], cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT))
    try:
        if not wait_health(BASE + "/health", '"ok"'):
            print("server non partito:\n" + (DATA / "server.log").read_text(encoding="utf-8")[-2000:])
            return 1
        asyncio.run(main_async())
    finally:
        stop(procs)
        log.close()
    if all(RESULTS):
        shutil.rmtree(DATA, ignore_errors=True)
    (ROOT / "tests" / "config.scuola.yaml").unlink(missing_ok=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test per la classe superati ====")
    return 0 if all(RESULTS) else 1


def _port_open(port: int) -> bool:
    with socket.socket() as so:
        so.settimeout(0.3)
        return so.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    sys.exit(main())
