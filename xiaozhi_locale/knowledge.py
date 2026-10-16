"""Base di conoscenza: documenti caricati dal prof (PDF, Word, testo) e ricerca in locale.

Il modello ha lo strumento "cerca_documenti": quando una domanda riguarda gli argomenti dei documenti, cerca i pezzi
più pertinenti e risponde usando quelli (è il modello a decidere quando cercare). La ricerca è BM25 sulle parole,
con una semplificazione per l'italiano (accenti, parole vuote, radici): nessun modello in più, nessun servizio
esterno. Il testo dei documenti è salvato cifrato in data/knowledge/; ogni documento ha i ruoli che possono usarlo.
"""
from __future__ import annotations

import io
import json
import math
import re
import secrets
import time
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path

from .auth import JsonStore
from .tools import ToolSet

MAX_FILE = 20 * 1024 * 1024
MAX_CHARS = 2_000_000
CHUNK, OVERLAP = 900, 150
ROLES = ("admin", "user", "device")
STOP = set("""il lo la i gli le un uno una di a da in con su per tra fra e ed o od ma se che chi cui non piu più
del dello della dei degli delle al allo alla ai agli alle dal dallo dalla dai dagli dalle nel nello nella nei negli
nelle sul sullo sulla sui sugli sulle col coi è e' sono era erano essere ha hanno ho hai avere come anche quando dove
perche perché quale quali quanto questo questa questi queste quello quella quelli quelle si ci ne mi ti vi lo sua suo
suoi sue loro mio mia tuo tua cosa cose molto poco tutto tutti tutte sempre ancora già gia poi dopo prima così cosi
the of and to in is are a an for on with as by at from that this it be or""".split())


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    """Parole utili, ridotte a una radice: "regolamento", "regolamenti" e "regole" si somigliano."""
    out = []
    for w in re.findall(r"[a-z0-9]{2,}", _norm(text)):
        if w in STOP:
            continue
        out.append(w[:6] if len(w) > 7 else w)
    return out


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", " ", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        import html
        return html.unescape(text)
    if name.endswith((".txt", ".md", ".csv")):
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    if name.endswith((".html", ".htm")):
        import html
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", data.decode("utf-8", "replace"))
        return html.unescape(re.sub(r"<[^>]+>", " ", text))
    raise ValueError("Formato non supportato: carica PDF, Word (.docx), testo (.txt, .md) o pagine web (.html)")


def chunk_text(text: str) -> list[str]:
    """Pezzi di ~900 caratteri che finiscono a fine frase, un po' sovrapposti (il contesto non si perde)."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + CHUNK)
        if end < len(text):
            cut = max(text.rfind(". ", i + CHUNK // 2, end), text.rfind("\n", i + CHUNK // 2, end))
            if cut > 0:
                end = cut + 1
        piece = text[i:end].strip()
        if len(piece) > 30:
            out.append(piece)
        if end >= len(text):
            break
        i = max(end - OVERLAP, i + 1)
    return out


class Knowledge:
    def __init__(self, data_dir: Path, box=None, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.dir = Path(data_dir) / "knowledge"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store = JsonStore(Path(data_dir) / "knowledge.json")
        self.store.data.setdefault("docs", [])
        self.box = box
        self.chunks: dict[str, list[str]] = {}
        self._index: list[tuple[str, int, Counter, int]] = []   # (doc, n, parole, lunghezza)
        self._df: Counter = Counter()
        for d in self.docs():
            try:
                self.chunks[d["id"]] = self._read(d["id"])
            except Exception:
                self.chunks[d["id"]] = []
        self._reindex()

    # ------------------------------------------------------------------ archivio
    def docs(self) -> list[dict]:
        return self.store.data["docs"]

    def _read(self, did: str) -> list[str]:
        raw = (self.dir / f"{did}.json").read_text(encoding="utf-8")
        return json.loads(self.box.decrypt(raw) if self.box else raw)

    def _write(self, did: str, chunks: list[str]):
        raw = json.dumps(chunks, ensure_ascii=False)
        (self.dir / f"{did}.json").write_text(self.box.encrypt(raw) if self.box else raw, encoding="utf-8")

    def _reindex(self):
        idx, df = [], Counter()
        for d in self.docs():
            for n, c in enumerate(self.chunks.get(d["id"], [])):
                toks = tokens(d["title"] + " " + c)
                cnt = Counter(toks)
                idx.append((d["id"], n, cnt, len(toks)))
                df.update(cnt.keys())
        self._index, self._df = idx, df

    def add(self, filename: str, data: bytes, title: str = "", roles: list[str] | None = None) -> dict:
        if len(data) > MAX_FILE:
            raise ValueError("File troppo grande (massimo 20 MB)")
        try:
            text = extract_text(filename, data)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Non riesco a leggere il file: {e}") from None
        text = text[:MAX_CHARS]
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Nel file non c'è testo da leggere (un PDF fatto di sole immagini va prima convertito "
                             "in testo con un programma di riconoscimento del testo)")
        roles = [r for r in (roles or ROLES) if r in ROLES] or list(ROLES)
        did = secrets.token_hex(4)
        title = " ".join(str(title or Path(filename).stem).split())[:120]
        self._write(did, chunks)
        doc = {"id": did, "title": title, "filename": Path(filename).name[:120], "roles": roles,
               "chars": len(text), "chunks": len(chunks), "created": int(time.time())}
        with self.store.lock:
            self.store.data["docs"].append(doc)
            self.store.save()
        self.chunks[did] = chunks
        self._reindex()
        return doc

    def update(self, did: str, title: str | None = None, roles: list[str] | None = None) -> dict:
        d = next((x for x in self.docs() if x["id"] == did), None)
        if d is None:
            raise ValueError("Documento inesistente")
        with self.store.lock:
            if title:
                d["title"] = " ".join(str(title).split())[:120]
            if roles is not None:
                d["roles"] = [r for r in roles if r in ROLES] or d["roles"]
            self.store.save()
        self._reindex()
        return d

    def delete(self, did: str) -> bool:
        before = len(self.docs())
        with self.store.lock:
            self.store.data["docs"] = [x for x in self.docs() if x["id"] != did]
            self.store.save()
        (self.dir / f"{did}.json").unlink(missing_ok=True)
        self.chunks.pop(did, None)
        self._reindex()
        return len(self.docs()) < before

    # ------------------------------------------------------------------ ricerca
    def search(self, query: str, role: str, k: int = 3) -> list[dict]:
        allowed = {d["id"]: d for d in self.docs() if role in d.get("roles", ROLES)}
        q = set(tokens(query))
        if not q or not allowed or not self._index:
            return []
        n = len(self._index)
        avg = sum(x[3] for x in self._index) / n
        k1, b = 1.4, 0.75
        scored = []
        for did, num, cnt, length in self._index:
            if did not in allowed:
                continue
            s = 0.0
            for w in q:
                f = cnt.get(w)
                if f:
                    idf = math.log(1 + (n - self._df[w] + 0.5) / (self._df[w] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * length / avg))
            if s > 0:
                scored.append((s, did, num))
        scored.sort(reverse=True)
        return [{"doc": allowed[did]["title"], "part": num + 1, "score": round(s, 2), "text": self.chunks[did][num]}
                for s, did, num in scored[:k]]

    def context_for(self, text: str, role: str) -> str:
        """Pezzi molto pertinenti da mettere accanto alla domanda (knowledge.auto, di serie acceso)."""
        if not self.cfg.get("auto", True) or len(set(tokens(text))) < 2:
            return ""
        q = set(tokens(text))
        # pertinente = contiene almeno 2 parole importanti della domanda e almeno il 40% (non dipende da quanti
        # documenti ci sono, a differenza del punteggio)
        need = max(2, round(len(q) * float(self.cfg.get("auto_min_overlap", 0.4))))
        hits = [h for h in self.search(text, role, 2) if len(q & set(tokens(h["doc"] + " " + h["text"]))) >= need]
        if not hits:
            return ""
        parts = "\n".join(f"[{h['doc']}] {h['text']}" for h in hits)
        return ("\n\n(Dai documenti del prof, possono servire per rispondere; se non c'entrano ignorali:\n"
                f"{parts})")

    def toolset(self, role: str) -> ToolSet:
        ts = ToolSet()
        titles = [d["title"] for d in self.docs() if role in d.get("roles", ROLES)]
        if not titles:
            return ts

        async def find(_n, a):
            hits = self.search(str(a.get("domanda", "")), role, int(self.cfg.get("results", 3)))
            if not hits:
                return "Nei documenti non ho trovato niente su questo. Dillo all'utente, senza inventare."
            parts = [f"[{h['doc']}, parte {h['part']}]\n{h['text']}" for h in hits]
            return ("Pezzi dei documenti caricati dal prof. Rispondi usando queste informazioni e cita il documento; "
                    "se non bastano, dillo.\n\n" + "\n\n".join(parts))
        names = ", ".join(titles[:12]) + ("…" if len(titles) > 12 else "")
        # descrizione provata con il modello piccolo: senza "informazioni che NON conosci" risponde inventando
        ts.add("cerca_documenti", "Cerca nei documenti della scuola caricati dal prof. Contengono informazioni che tu NON "
               "conosci: regole, procedure, date, avvisi, contenuti delle lezioni. Usalo sempre prima di rispondere a "
               f"domande che possono riguardare questi documenti, senza inventare: {names}",
               {"type": "object", "properties": {"domanda": {"type": "string", "maxLength": 300,
                                                             "description": "cosa cercare, con le parole importanti"}},
                "required": ["domanda"]}, find)
        return ts

    def public(self) -> list[dict]:
        return self.docs()
