"""Strumenti personalizzati, creati dall'admin dalla pagina web: comandi, chiamate web, sensori MQTT.

Tutto è SPENTO finché l'admin non lo accende (per tipo e per singolo strumento). Regole di sicurezza:
  * comandi: si esegue un programma con argomenti fissi, mai una shell. I valori scelti dal modello
    finiscono solo nei posti segnati con {parametro} e devono rispettare il loro schema (tipo, valori
    ammessi, espressione regolare): niente "; rm -rf" o simili;
  * web: l'indirizzo (sito) è fisso, i valori del modello vanno solo nel percorso o nella query, codificati;
  * ogni strumento dice chi può usarlo (admin, utenti, dispositivi); ogni uso finisce nel registro eventi;
  * i siti esterni attivati compaiono nella pagina Admin, sezione Privacy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from .auth import JsonStore
from .config import ROOT
from .tools import ToolSet

log = logging.getLogger("custom_tools")

KINDS = {
    "command": "Comandi sul server",
    "web": "Chiamate a siti web",
    "mqtt": "Sensori e dispositivi MQTT",
}
ROLES = ("admin", "user", "device")
TYPES = ("string", "integer", "number", "boolean")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# per i comandi un testo può contenere solo caratteri innocui (niente ; | & $ ` > < * ? e simili)
SAFE_COMMAND_TEXT = r"^[\w .,:@+\-/]{0,64}$"
MAX_RESPONSE = 200_000

PRESETS = [
    {"preset": "meteo", "title": "Meteo di oggi", "kind": "web", "name": "meteo_oggi",
     "description": "Meteo di oggi nella città della scuola: temperatura, vento, pioggia. "
                    "weather_code segue lo standard WMO (0 sereno, 1-3 nuvoloso, 45-48 nebbia, 51-67 pioggia, "
                    "71-77 neve, 80-82 rovesci, 95-99 temporale).",
     "params": [], "roles": ["admin", "user", "device"], "method": "GET",
     "url": "https://api.open-meteo.com/v1/forecast?latitude=45.46&longitude=9.19&current=temperature_2m,"
            "weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max&timezone=auto&forecast_days=1",
     "extract": "current,daily",
     "note": "Cambia latitude e longitude nell'indirizzo con quelle della tua città. Invia al sito solo "
             "le coordinate, nessun dato degli utenti."},
    {"preset": "wikipedia", "title": "Wikipedia (sintesi)", "kind": "web", "name": "wikipedia",
     "description": "Riassunto di una voce di Wikipedia in italiano. Usalo per definizioni e spiegazioni.",
     "params": [{"name": "argomento", "type": "string", "description": "Titolo della voce, es. Leonardo da Vinci",
                 "maxLength": 100}],
     "roles": ["admin", "user", "device"], "method": "GET",
     "url": "https://it.wikipedia.org/api/rest_v1/page/summary/{argomento}", "extract": "extract",
     "note": "Invia a Wikipedia solo l'argomento cercato."},
    {"preset": "aria", "title": "Qualità dell'aria", "kind": "web", "name": "qualita_aria",
     "description": "Qualità dell'aria adesso nella città della scuola: indice europeo (0-20 buona, 20-40 discreta, "
                    "40-60 moderata, 60-80 scarsa, oltre 80 molto scarsa), PM10 e PM2.5 in microgrammi per metro cubo.",
     "params": [], "roles": ["admin", "user", "device"], "method": "GET",
     "url": "https://air-quality-api.open-meteo.com/v1/air-quality?latitude=45.46&longitude=9.19"
            "&current=european_aqi,pm10,pm2_5&timezone=auto",
     "extract": "current",
     "note": "Cambia latitude e longitude con quelle della tua città. Invia al sito solo le coordinate."},
    {"preset": "allerte", "title": "Allerte meteo della regione", "kind": "web", "name": "allerte_meteo",
     "description": "Allerte meteo in corso nella regione della scuola (gialla, arancione, rossa: temporali, "
                    "pioggia, neve, vento, nebbia). Se non ce ne sono, dillo.",
     "params": [], "roles": ["admin", "user", "device"], "method": "GET",
     "url": "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy", "extract": "rss Lombardia",
     "note": "Nel campo \"Campi da tenere\" cambia Lombardia con la tua regione. Fonte: MeteoAlarm (avvisi "
             "ufficiali). Per le allerte di protezione civile del comune fa sempre fede il sito ufficiale."},
    {"preset": "notizie", "title": "Titoli delle notizie", "kind": "web", "name": "notizie",
     "description": "Ultimi titoli delle notizie italiane. Leggi solo i titoli, in breve.",
     "params": [], "roles": ["admin", "user", "device"], "method": "GET",
     "url": "https://www.ansa.it/sito/notizie/topnews/topnews_rss.xml", "extract": "rss",
     "note": "Legge solo i titoli dal feed pubblico. Puoi usare qualsiasi altro feed RSS cambiando l'indirizzo."},
    {"preset": "disco", "title": "Spazio su disco del server", "kind": "command", "name": "spazio_disco",
     "description": "Dice quanto spazio libero c'è sul disco del server.", "params": [],
     "roles": ["admin"], "argv": ["{python}", "scripts/tools/spazio_disco.py"]},
    {"preset": "temperatura", "title": "Temperatura del processore", "kind": "command", "name": "temperatura_cpu",
     "description": "Temperatura del processore del server (Raspberry Pi e Linux).", "params": [],
     "roles": ["admin"], "argv": ["{python}", "scripts/tools/temperatura_cpu.py"]},
    {"preset": "ping", "title": "Il dispositivo è acceso?", "kind": "command", "name": "ping_dispositivo",
     "description": "Controlla se un dispositivo della rete risponde (ping).",
     "params": [{"name": "indirizzo", "type": "string", "description": "Indirizzo IP o nome, es. 192.168.1.50",
                 "pattern": r"^[A-Za-z0-9.\-]{1,64}$"}],
     "roles": ["admin"], "argv": ["{python}", "scripts/tools/ping.py", "{indirizzo}"]},
    {"preset": "sensore", "title": "Leggi un sensore (MQTT)", "kind": "mqtt", "name": "temperatura_aula",
     "description": "Ultimo valore del sensore di temperatura dell'aula.", "params": [],
     "roles": ["admin", "user", "device"], "action": "read", "broker": "builtin", "topic": "scuola/aula1/temperatura",
     "note": "Cambia nome, descrizione e argomento (topic) con quelli del tuo sensore."},
    {"preset": "lorawan", "title": "Sensore LoRaWAN (The Things Network)", "kind": "mqtt", "name": "temperatura_esterna",
     "description": "Ultima temperatura misurata dal sensore LoRaWAN in giardino, in gradi.", "params": [],
     "roles": ["admin", "user", "device"], "action": "read", "broker": "", "topic": "v3/ID-APP@ttn/devices/ID-SENSORE/up",
     "extract": "uplink_message.decoded_payload.temperature",
     "note": "Prima collega The Things Network in MQTT e sensori → Broker esterni (porta 8883, TLS, utente ID-APP@ttn, "
             "chiave API). Poi scegli quel broker qui, cambia ID-APP e ID-SENSORE e il campo da leggere."},
    {"preset": "attuatore", "title": "Comanda un dispositivo (MQTT)", "kind": "mqtt", "name": "luce_aula",
     "description": "Accende o spegne la luce dell'aula.",
     "params": [{"name": "stato", "type": "string", "description": "on per accendere, off per spegnere",
                 "enum": ["on", "off"]}],
     "roles": ["admin"], "action": "write", "broker": "builtin", "topic": "scuola/aula1/luce/set", "payload": "{stato}",
     "note": "Il dispositivo (es. ESP8266) deve essere iscritto a questo argomento."},
]


class ToolError(ValueError):
    pass


def _schema(params: list[dict]) -> dict:
    props, required = {}, []
    for p in params:
        spec = {"type": p["type"], "description": p.get("description", "")}
        for k in ("enum", "minimum", "maximum", "maxLength"):
            if k in p:
                spec[k] = p[k]
        props[p["name"]] = spec
        if p.get("required", True):
            required.append(p["name"])
    return {"type": "object", "properties": props, "required": required}


def _fill(template: str, values: dict, encode=None) -> str:
    def sub(m):
        if m.group(1) == "python":
            return sys.executable
        if m.group(1) not in values:
            raise ToolError(f"manca il valore per {m.group(1)}")
        v = values[m.group(1)]
        v = str(v).lower() if isinstance(v, bool) else str(v)
        return encode(v) if encode else v
    return PLACEHOLDER_RE.sub(sub, template)


def _extract(data, paths: str):
    if not paths:
        return data
    out = {}
    for path in [p.strip() for p in paths.split(",") if p.strip()]:
        cur = data
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        out[path] = cur
    return out[paths.strip()] if len(out) == 1 else out


def _rss_titles(text: str, keyword: str = "", limit: int = 8) -> str:
    """Titoli di un feed RSS/Atom (es. notizie, allerte meteo). Con `keyword` solo quelli che la contengono.

    Si leggono solo i <title> con un'espressione regolare: nessun parser XML su contenuti di siti esterni."""
    import html

    titles = [html.unescape(re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.strip())).strip()
              for m in re.findall(r"<title[^>]*>(.*?)</title>", text, re.S)]
    titles = [x for x in titles[1:] if x]  # il primo è il nome del feed
    if keyword:
        titles = [x for x in titles if keyword.lower() in x.lower()]
        if not titles:
            return f"Nessuna voce che riguarda {keyword}."
    return "\n".join(f"- {x}" for x in titles[:limit]) or "Nessuna voce."


class CustomTools:
    def __init__(self, data_dir: Path, box=None, cfg: dict | None = None):
        self.store = JsonStore(Path(data_dir) / "custom_tools.json")
        self.box = box
        self.cfg = cfg or {}
        self.mqtt = None  # impostato dall'app: ponte MQTT per gli strumenti di tipo "mqtt"
        self.store.data.setdefault("kinds", {k: False for k in KINDS})
        self.store.data.setdefault("tools", [])

    # ------------------------------------------------------------------ stato
    def kinds(self) -> dict:
        return {k: bool(self.store.data["kinds"].get(k)) for k in KINDS}

    def set_kind(self, kind: str, enabled: bool):
        if kind not in KINDS:
            raise ToolError("tipo sconosciuto")
        with self.store.lock:
            self.store.data["kinds"][kind] = bool(enabled)
            self.store.save()

    def _find(self, tool_id: str) -> dict | None:
        return next((t for t in self.store.data["tools"] if t["id"] == tool_id), None)

    def public(self) -> list[dict]:
        """Per la pagina Admin: i valori segreti delle intestazioni non vengono mai rimandati al browser."""
        out = []
        for t in self.store.data["tools"]:
            v = {k: v for k, v in t.items() if k != "headers"}
            v["headers"] = {k: "••••••" for k in (t.get("headers") or {})}
            v["active"] = bool(t.get("enabled")) and self.kinds()[t["kind"]]
            out.append(v)
        return out

    def external_hosts(self) -> list[dict]:
        """Siti esterni contattati dagli strumenti web attivi (per la tabella Privacy)."""
        if not self.kinds()["web"]:
            return []
        return [{"what": f"Strumento web '{t['name']}'", "url": urlsplit(t["url"]).netloc}
                for t in self.store.data["tools"] if t["kind"] == "web" and t.get("enabled")]

    # ------------------------------------------------------------------ creazione e modifica
    def validate(self, d: dict) -> dict:
        kind = d.get("kind")
        if kind not in KINDS:
            raise ToolError("Tipo di strumento sconosciuto")
        name = str(d.get("name", "")).strip().lower()
        if not NAME_RE.match(name):
            raise ToolError("Nome non valido: lettere minuscole, numeri e _ (es. meteo_oggi)")
        desc = str(d.get("description", "")).strip()
        if not desc or len(desc) > 600:
            raise ToolError("Scrivi una descrizione (serve al modello per capire quando usarlo)")
        roles = [r for r in d.get("roles") or [] if r in ROLES] or ["admin"]
        params = []
        for p in d.get("params") or []:
            pname = str(p.get("name", "")).strip().lower()
            if not PARAM_RE.match(pname):
                raise ToolError(f"Nome di parametro non valido: {pname!r}")
            ptype = p.get("type", "string")
            if ptype not in TYPES:
                raise ToolError(f"Tipo non valido per {pname}: usa {', '.join(TYPES)}")
            q = {"name": pname, "type": ptype, "description": str(p.get("description", ""))[:200],
                 "required": bool(p.get("required", True))}
            if p.get("enum"):
                q["enum"] = [str(x) if ptype == "string" else x for x in p["enum"]][:50]
            for k in ("minimum", "maximum"):
                if p.get(k) not in (None, ""):
                    q[k] = float(p[k])
            if ptype == "string":
                q["maxLength"] = int(p.get("maxLength") or 100)
                pattern = p.get("pattern") or (SAFE_COMMAND_TEXT if kind == "command" else "")
                if pattern:
                    re.compile(pattern)
                    q["pattern"] = pattern
            params.append(q)
        names = {p["name"] for p in params}
        tool = {"id": d.get("id") or secrets.token_hex(4), "kind": kind, "name": name, "description": desc,
                "params": params, "roles": roles, "enabled": bool(d.get("enabled", True))}

        def check_placeholders(*texts):
            for text in texts:
                for ph in PLACEHOLDER_RE.findall(text or ""):
                    if ph != "python" and ph not in names:
                        raise ToolError(f"{{{ph}}} non corrisponde a nessun parametro")

        if kind == "command":
            argv = [str(a) for a in d.get("argv") or [] if str(a).strip()]
            if not argv or len(argv) > 30:
                raise ToolError("Scrivi il comando: il programma e i suoi argomenti, uno per riga")
            if PLACEHOLDER_RE.search(argv[0].replace("{python}", "")):
                raise ToolError("Il programma da eseguire deve essere fisso (niente {parametri} nella prima riga)")
            check_placeholders(*argv)
            tool["argv"] = argv
            tool["timeout"] = max(1, min(60, int(d.get("timeout") or 15)))
        elif kind == "web":
            url = str(d.get("url", "")).strip()
            parts = urlsplit(PLACEHOLDER_RE.sub("x", url))
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ToolError("Indirizzo non valido: deve iniziare con https:// (o http://)")
            if "{" in urlsplit(url).netloc or "{" in urlsplit(url).scheme:
                raise ToolError("Il sito deve essere fisso: i {parametri} vanno solo nel percorso o dopo il ?")
            method = str(d.get("method", "GET")).upper()
            if method not in ("GET", "POST"):
                raise ToolError("Metodo: GET o POST")
            body = str(d.get("body") or "")
            check_placeholders(url, body)
            tool.update({"url": url, "method": method, "body": body, "extract": str(d.get("extract") or "")[:200]})
            old = self._find(tool["id"]) or {}
            headers = {}
            for k, v in (d.get("headers") or {}).items():
                k = str(k).strip()
                if not k:
                    continue
                if v == "••••••" and k in (old.get("headers") or {}):
                    headers[k] = old["headers"][k]  # valore segreto non cambiato
                else:
                    headers[k] = self.box.encrypt(str(v)) if self.box else str(v)
            tool["headers"] = headers
        else:  # mqtt
            action = d.get("action")
            topic = str(d.get("topic", "")).strip()
            if action not in ("read", "write"):
                raise ToolError("Azione MQTT: read (leggi) o write (comanda)")
            if not topic or len(topic) > 200 or (action == "write" and any(c in topic for c in "+#")):
                raise ToolError("Argomento (topic) MQTT non valido")
            payload = str(d.get("payload") or "")
            check_placeholders(topic, payload)
            tool.update({"action": action, "topic": topic, "payload": payload,
                         "broker": str(d.get("broker") or "builtin"), "extract": str(d.get("extract") or "")[:200]})
        return tool

    def save(self, d: dict) -> dict:
        tool = self.validate(d)
        with self.store.lock:
            tools = self.store.data["tools"]
            if any(t["name"] == tool["name"] and t["id"] != tool["id"] for t in tools):
                raise ToolError(f"Esiste già uno strumento chiamato {tool['name']}")
            idx = next((i for i, t in enumerate(tools) if t["id"] == tool["id"]), None)
            if idx is None:
                tools.append(tool)
            else:
                tools[idx] = tool
            self.store.save()
        return tool

    def delete(self, tool_id: str) -> bool:
        with self.store.lock:
            before = len(self.store.data["tools"])
            self.store.data["tools"] = [t for t in self.store.data["tools"] if t["id"] != tool_id]
            self.store.save()
            return len(self.store.data["tools"]) < before

    def set_enabled(self, tool_id: str, enabled: bool):
        with self.store.lock:
            t = self._find(tool_id)
            if not t:
                raise ToolError("Strumento inesistente")
            t["enabled"] = bool(enabled)
            self.store.save()

    # ------------------------------------------------------------------ uso da parte del modello
    def toolset_for(self, role: str, kind: str | None = None) -> ToolSet:
        ts = ToolSet()
        kinds = self.kinds()
        for t in self.store.data["tools"]:
            if kind and t["kind"] != kind:
                continue
            if t.get("enabled") and kinds.get(t["kind"]) and role in t.get("roles", []):
                ts.add(t["name"], t["description"], _schema(t["params"]), self._caller(t["id"]))
        return ts

    def _caller(self, tool_id: str):
        async def call(_name: str, args: dict) -> str:
            t = self._find(tool_id)
            if t is None or not t.get("enabled") or not self.kinds().get(t["kind"]):
                return "Errore: strumento disattivato"
            return await self.run(t, args)
        return call

    async def run(self, t: dict, args: dict) -> str:
        # i testi devono rispettare l'espressione regolare del parametro (ToolSet ha già controllato il resto)
        for p in t["params"]:
            if p["name"] in args and p.get("pattern") and not re.fullmatch(p["pattern"], str(args[p["name"]])):
                return f"Errore: valore non permesso per {p['name']}"
        try:
            if t["kind"] == "command":
                return await self._run_command(t, args)
            if t["kind"] == "web":
                return await self._run_web(t, args)
            return await self._run_mqtt(t, args)
        except ToolError as e:
            return f"Errore: {e}"

    async def _run_command(self, t: dict, args: dict) -> str:
        argv = [_fill(a, args) for a in t["argv"]]
        limit = int((self.cfg or {}).get("max_output_chars", 2000))
        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            return f"Errore: impossibile avviare il comando ({e})"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=t.get("timeout", 15))
        except asyncio.TimeoutError:
            proc.kill()
            return "Errore: il comando ci ha messo troppo"
        text = out.decode("utf-8", "replace").strip()[:limit]
        return text or ("Fatto." if proc.returncode == 0 else f"Errore (codice {proc.returncode})")

    async def _run_web(self, t: dict, args: dict) -> str:
        import aiohttp

        url = _fill(t["url"], args, encode=lambda v: quote(v, safe=""))
        if urlsplit(url).netloc != urlsplit(t["url"]).netloc:
            raise ToolError("indirizzo non permesso")
        headers = {k: (self.box.decrypt(v) if self.box else v) for k, v in (t.get("headers") or {}).items()}
        headers.setdefault("User-Agent", "husky-locale")
        body = _fill(t.get("body") or "", args, encode=lambda v: json.dumps(v)[1:-1]) if t["method"] == "POST" else None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            async with http.request(t["method"], url, headers=headers, data=body.encode() if body else None,
                                    allow_redirects=False) as r:
                raw = await r.content.read(MAX_RESPONSE)
                if r.status >= 400:
                    return f"Errore: il sito ha risposto {r.status}"
        text = raw.decode("utf-8", "replace")
        extract = t.get("extract", "")
        if extract.startswith("rss"):
            return _rss_titles(text, extract[4:].strip())
        try:
            data = _extract(json.loads(text), extract)
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        except ValueError:
            pass
        return text[: int((self.cfg or {}).get("max_output_chars", 2000))]

    async def _run_mqtt(self, t: dict, args: dict) -> str:
        if self.mqtt is None:
            return "Errore: MQTT non è attivo sul server"
        topic = _fill(t["topic"], args)
        if t["action"] == "read":
            value = self.mqtt.last_value(t.get("broker", "builtin"), topic)
            if value is None:
                return f"Nessun valore ricevuto finora su {topic}"
            payload, when = value
            if t.get("extract"):  # es. LoRaWAN: uplink_message.decoded_payload.temperature
                try:
                    picked = _extract(json.loads(payload), t["extract"])
                    payload = picked if isinstance(picked, str) else json.dumps(picked, ensure_ascii=False)
                except ValueError:
                    pass
            age = int(time.time() - when)
            return f"{payload} (ricevuto {age} secondi fa)" if age < 120 else f"{payload} (ricevuto {age // 60} minuti fa)"
        await self.mqtt.publish(t.get("broker", "builtin"), topic, _fill(t.get("payload") or "", args))
        return "Comando inviato."
