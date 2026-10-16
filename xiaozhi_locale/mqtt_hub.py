"""MQTT per Husky: broker integrato, broker esterni, ultimi valori dei sensori, dispositivi ESPHome.

Si gestisce tutto dalla pagina Admin (sezione MQTT) e parte SPENTO. Le impostazioni stanno in data/mqtt.json
(password degli account come hash, password dei broker esterni cifrate).

ESPHome: se nel file YAML del dispositivo c'è il blocco `mqtt:` con discovery attivo (predefinito), il dispositivo
annuncia da solo i suoi sensori e comandi su homeassistant/.../config: Husky li riconosce e crea gli strumenti.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path

from .auth import JsonStore, hash_password, verify_password
from .mqtt_broker import Broker, ClientConn
from .mqtt_client import MqttClient
from .tools import ToolSet

log = logging.getLogger("mqtt")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
MAX_TOPICS = 5000
# abbreviazioni usate nei messaggi di discovery (stesso formato di Home Assistant, usato da ESPHome)
ABBR = {"stat_t": "state_topic", "cmd_t": "command_topic", "unit_of_meas": "unit_of_measurement",
        "dev_cla": "device_class", "uniq_id": "unique_id", "obj_id": "object_id", "dev": "device",
        "avty_t": "availability_topic", "pl_on": "payload_on", "pl_off": "payload_off", "pl_prs": "payload_press",
        "pl_avail": "payload_available", "pl_not_avail": "payload_not_available", "ops": "options",
        "bri_scl": "brightness_scale", "stat_on": "state_on", "stat_off": "state_off", "ids": "identifiers",
        "mdl": "model", "mf": "manufacturer", "sw": "sw_version"}
READ_KINDS = ("sensor", "binary_sensor", "text_sensor")
COMMAND_KINDS = ("switch", "light", "fan", "button", "number", "select")


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", s.lower()).strip("_")[:30] or "x"


def expand_discovery(cfg: dict) -> dict:
    out = {ABBR.get(k, k): v for k, v in cfg.items()}
    if isinstance(out.get("device"), dict):
        out["device"] = {ABBR.get(k, k): v for k, v in out["device"].items()}
    base = out.pop("~", None)
    if base:
        for k, v in list(out.items()):
            if k.endswith("_topic") and isinstance(v, str):
                out[k] = base + v[1:] if v.startswith("~") else (v[:-1] + base if v.endswith("~") else v)
    return out


class MqttHub:
    def __init__(self, data_dir: Path, cfg: dict, auth, box=None):
        self.cfg = cfg or {}
        self.store = JsonStore(Path(data_dir) / "mqtt.json")
        self.store.data.setdefault("enabled", False)
        self.store.data.setdefault("accounts", [])
        self.store.data.setdefault("external", [])
        self.auth = auth
        self.box = box
        self.broker: Broker | None = None
        self.external: dict[str, MqttClient] = {}
        self.values: dict[tuple[str, str], tuple[str, float]] = {}
        self.esphome: dict[str, dict] = {}  # entità ESPHome riconosciute, per unique_id
        self.gateways = []  # gestori aggiuntivi (protocollo xiaozhi MQTT+UDP)

    # ------------------------------------------------------------------ avvio e arresto
    @property
    def enabled(self) -> bool:
        return bool(self.store.data.get("enabled"))

    async def start(self):
        if self.enabled and self.broker is None:
            self.broker = Broker(self._authenticate, int(self.cfg.get("max_clients", 200)),
                                 int(self.cfg.get("max_per_ip", 30)), int(self.cfg.get("messages_per_second", 50)))
            self.broker.listeners.append(self._on_builtin)
            for g in self.gateways:
                g.attach(self.broker)
            try:
                await self.broker.start(self.cfg.get("host", "0.0.0.0"), int(self.cfg.get("port", 1883)))
            except OSError as e:
                log.error("Broker MQTT non avviato (porta %s occupata?): %s", self.cfg.get("port", 1883), e)
                self.broker = None
                return
            for g in self.gateways:
                await g.start()
        for ext in self.store.data["external"]:
            if ext.get("enabled", True) and ext["id"] not in self.external:
                self._start_external(ext)

    async def stop(self):
        for g in self.gateways:
            await g.stop()
        if self.broker:
            await self.broker.stop()
            self.broker = None
        for c in self.external.values():
            await c.stop()
        self.external.clear()

    async def set_enabled(self, on: bool):
        with self.store.lock:
            self.store.data["enabled"] = bool(on)
            self.store.save()
        await self.stop()
        if on:
            await self.start()

    def _start_external(self, ext: dict):
        async def on_msg(topic, payload, _id=ext["id"]):
            self._remember(_id, topic, payload)
        pw = self.box.decrypt(ext["password"]) if (self.box and ext.get("password")) else ext.get("password")
        c = MqttClient(ext["host"], int(ext.get("port") or 1883), bool(ext.get("tls")), ext.get("username") or None,
                       pw or None, f"husky-{secrets.token_hex(3)}", ext.get("topics") or ["#"], on_msg)
        c.start()
        self.external[ext["id"]] = c

    # ------------------------------------------------------------------ accesso al broker integrato
    async def _authenticate(self, client_id: str, username: str | None, password: str | None, ip: str) -> dict | None:
        parts = client_id.split("@@@")
        if len(parts) == 3:  # dispositivo xiaozhi: GID@@@mac_con_trattini_bassi@@@client-id, password = suo token
            mac, uuid = parts[1].replace("_", ":"), parts[2]
            if self.auth.device_ok(mac, uuid, password or ""):
                return {"kind": "xiaozhi", "pub": ["device-server"], "sub": [], "mac": mac, "uuid": uuid}
            return None
        for a in self.store.data["accounts"]:
            if a["username"] == username and verify_password(password or "", a["password"]):
                return {"kind": "account", "pub": a.get("pub") or ["#"], "sub": a.get("sub") or ["#"],
                        "account": username}
        return None

    def accounts(self) -> list[dict]:
        return [{k: v for k, v in a.items() if k != "password"} for a in self.store.data["accounts"]]

    def add_account(self, username: str, password: str, pub=None, sub=None, note: str = "") -> str | None:
        if not USERNAME_RE.match(username or ""):
            return "Nome account: 3-40 caratteri tra lettere, numeri, punto, trattino"
        if len(password or "") < 10:
            return "La password dell'account MQTT deve avere almeno 10 caratteri"
        with self.store.lock:
            if any(a["username"] == username for a in self.store.data["accounts"]):
                return "Account già esistente"
            self.store.data["accounts"].append({"username": username, "password": hash_password(password),
                                                "pub": pub or ["#"], "sub": sub or ["#"], "note": note[:100],
                                                "created": time.strftime("%Y-%m-%d %H:%M")})
            self.store.save()
        return None

    async def remove_account(self, username: str) -> bool:
        with self.store.lock:
            before = len(self.store.data["accounts"])
            self.store.data["accounts"] = [a for a in self.store.data["accounts"] if a["username"] != username]
            self.store.save()
        if self.broker:  # chi era collegato con quell'account viene scollegato subito
            for c in list(self.broker.clients.values()):
                if c.info.get("account") == username:
                    await c.close()
        return len(self.store.data["accounts"]) < before

    # ------------------------------------------------------------------ broker esterni
    def external_list(self) -> list[dict]:
        out = []
        for e in self.store.data["external"]:
            c = self.external.get(e["id"])
            out.append({**{k: v for k, v in e.items() if k != "password"}, "has_password": bool(e.get("password")),
                        "connected": bool(c and c.connected), "error": c.last_error if c else ""})
        return out

    async def save_external(self, d: dict) -> str | None:
        host = str(d.get("host", "")).strip()
        if not re.match(r"^[A-Za-z0-9.\-]{1,253}$", host):
            return "Indirizzo del broker non valido"
        topics = [t.strip() for t in d.get("topics") or [] if t.strip()][:20]
        from . import mqtt_proto as mp
        if not topics or not all(mp.valid_filter(t) for t in topics):
            return "Scrivi almeno un argomento valido (es. v3/+/devices/+/up)"
        ext = {"id": d.get("id") or secrets.token_hex(4), "name": str(d.get("name") or host)[:60], "host": host,
               "port": int(d.get("port") or (8883 if d.get("tls") else 1883)), "tls": bool(d.get("tls")),
               "username": str(d.get("username") or "")[:200], "topics": topics, "enabled": True}
        old = next((e for e in self.store.data["external"] if e["id"] == ext["id"]), {})
        pw = d.get("password")
        ext["password"] = old.get("password", "") if pw in (None, "", "••••••") else (self.box.encrypt(pw) if self.box else pw)
        with self.store.lock:
            self.store.data["external"] = [e for e in self.store.data["external"] if e["id"] != ext["id"]] + [ext]
            self.store.save()
        if ext["id"] in self.external:
            await self.external.pop(ext["id"]).stop()
        self._start_external(ext)
        return None

    async def remove_external(self, ext_id: str):
        with self.store.lock:
            self.store.data["external"] = [e for e in self.store.data["external"] if e["id"] != ext_id]
            self.store.save()
        if ext_id in self.external:
            await self.external.pop(ext_id).stop()

    def external_hosts(self) -> list[dict]:
        return [{"what": f"Broker MQTT '{e['name']}'", "url": f"{e['host']}:{e['port']}"} for e in self.store.data["external"]]

    # ------------------------------------------------------------------ valori e comandi
    def _remember(self, broker_id: str, topic: str, payload: bytes):
        if len(self.values) >= MAX_TOPICS and (broker_id, topic) not in self.values:
            oldest = min(self.values, key=lambda k: self.values[k][1])
            del self.values[oldest]
        self.values[(broker_id, topic)] = (payload[:2000].decode("utf-8", "replace"), time.time())

    def last_value(self, broker_id: str, topic: str):
        return self.values.get((broker_id, topic))

    async def publish(self, broker_id: str, topic: str, payload: str, retain: bool = False):
        if broker_id == "builtin":
            if not self.broker:
                raise RuntimeError("il broker MQTT integrato è spento")
            await self.broker.publish(topic, payload, retain)
        else:
            c = self.external.get(broker_id)
            if not c or not await c.publish(topic, payload, retain):
                raise RuntimeError("broker esterno non collegato")

    async def _on_builtin(self, source: ClientConn | None, topic: str, payload: bytes, retain: bool):
        if source is not None and source.info.get("kind") == "xiaozhi":
            return  # i messaggi dei dispositivi xiaozhi li gestisce il loro gateway
        self._remember("builtin", topic, payload)
        prefix = self.cfg.get("discovery_prefix", "homeassistant")
        if topic.startswith(prefix + "/") and topic.endswith("/config"):
            self._discovery(topic, payload)

    # ------------------------------------------------------------------ ESPHome (discovery)
    def _discovery(self, topic: str, payload: bytes):
        parts = topic.split("/")
        if len(parts) not in (4, 5):
            return
        kind = parts[1]
        if not payload:  # messaggio vuoto = entità rimossa
            self.esphome = {k: v for k, v in self.esphome.items() if v["config_topic"] != topic}
            return
        try:
            cfg = expand_discovery(json.loads(payload))
        except ValueError:
            return
        if kind not in READ_KINDS + COMMAND_KINDS:
            return
        dev = cfg.get("device") or {}
        node = parts[2] if len(parts) == 5 else (dev.get("name") or parts[2])
        uid = cfg.get("unique_id") or topic
        self.esphome[uid] = {"kind": kind, "node": _safe(str(node)), "device": dev.get("name") or node,
                             "name": cfg.get("name") or parts[-2], "object": _safe(cfg.get("object_id") or parts[-2]),
                             "config_topic": topic, "cfg": cfg}
        log.info("ESPHome: riconosciuto %s '%s' di %s", kind, self.esphome[uid]["name"], node)

    def esphome_devices(self) -> list[dict]:
        nodes: dict[str, dict] = {}
        for e in self.esphome.values():
            n = nodes.setdefault(e["node"], {"node": e["node"], "name": e["device"], "entities": []})
            st = self.last_value("builtin", e["cfg"].get("state_topic", "")) if e["cfg"].get("state_topic") else None
            n["entities"].append({"kind": e["kind"], "name": e["name"], "value": st[0] if st else None,
                                  "unit": e["cfg"].get("unit_of_measurement", "")})
        return sorted(nodes.values(), key=lambda n: n["node"])

    def _read_node(self, node: str) -> str:
        lines = []
        for e in self.esphome.values():
            if e["node"] != node or e["kind"] not in READ_KINDS + ("switch", "light", "fan"):
                continue
            st = self.last_value("builtin", e["cfg"].get("state_topic", ""))
            if not st:
                lines.append(f"{e['name']}: nessun valore ancora")
                continue
            value = st[0]
            if e["kind"] in ("light", "fan") and value.startswith("{"):
                try:
                    value = json.loads(value).get("state", value)
                except ValueError:
                    pass
            if e["kind"] in ("binary_sensor", "switch", "light", "fan"):
                value = {"ON": "acceso / sì", "OFF": "spento / no"}.get(str(value).upper(), value)
            unit = e["cfg"].get("unit_of_measurement", "")
            age = int(time.time() - st[1])
            lines.append(f"{e['name']}: {value}{(' ' + unit) if unit else ''} (di {age} s fa)")
        return "\n".join(lines) or "Nessun dato da questo dispositivo."

    async def _command(self, uid: str, args: dict) -> str:
        e = self.esphome.get(uid)
        if not e or not e["cfg"].get("command_topic"):
            return "Errore: comando non disponibile"
        c, kind = e["cfg"], e["kind"]
        if kind == "button":
            payload = c.get("payload_press", "PRESS")
        elif kind in ("number", "select"):
            payload = str(args.get("valore"))
        else:
            on = str(args.get("stato", "")).lower() in ("on", "acceso", "accendi", "true", "1")
            if kind == "light" and c.get("schema") == "json":
                body = {"state": "ON" if on else "OFF"}
                if on and args.get("luminosita") is not None:
                    body["brightness"] = round(int(args["luminosita"]) * int(c.get("brightness_scale", 255)) / 100)
                payload = json.dumps(body)
            else:
                payload = c.get("payload_on", "ON") if on else c.get("payload_off", "OFF")
        await self.publish("builtin", c["command_topic"], payload)
        return "Comando inviato."

    def toolset_for(self, role: str) -> ToolSet:
        """Strumenti creati dai dispositivi ESPHome: lettura per tutti, comandi solo per i ruoli permessi."""
        ts = ToolSet()
        if not self.broker:
            return ts
        command_roles = self.cfg.get("esphome_command_roles", ["admin", "device"])
        nodes = {e["node"]: e["device"] for e in self.esphome.values()}
        for node, device in nodes.items():
            names = [e["name"] for e in self.esphome.values() if e["node"] == node and e["kind"] in READ_KINDS]
            if names:
                async def read(_n, _a, _node=node):
                    return self._read_node(_node)
                ts.add(f"esphome_{node}", f"Legge i sensori del dispositivo {device}: {', '.join(names)[:300]}",
                       {"type": "object", "properties": {}}, read)
        if role not in command_roles:
            return ts
        for uid, e in self.esphome.items():
            kind, c = e["kind"], e["cfg"]
            if kind not in COMMAND_KINDS or not c.get("command_topic"):
                continue
            props: dict = {}
            if kind in ("switch", "light", "fan"):
                props["stato"] = {"type": "string", "enum": ["on", "off"], "description": "on per accendere, off per spegnere"}
                if kind == "light":
                    props["luminosita"] = {"type": "integer", "minimum": 1, "maximum": 100, "description": "percentuale (facoltativa)"}
            elif kind == "number":
                props["valore"] = {"type": "number", "description": c.get("unit_of_measurement", "")}
                for k in ("min", "max"):
                    if k in c:
                        props["valore"]["minimum" if k == "min" else "maximum"] = c[k]
            elif kind == "select":
                props["valore"] = {"type": "string", "enum": [str(o) for o in c.get("options", [])][:50]}
            verb = {"button": "Preme il pulsante", "number": "Imposta", "select": "Sceglie"}.get(kind, "Accende o spegne")
            required = [k for k in props if k != "luminosita"]

            async def cmd(_n, a, _uid=uid):
                return await self._command(_uid, a)
            ts.add(f"{e['node']}_{e['object']}", f"{verb} '{e['name']}' del dispositivo {e['device']}",
                   {"type": "object", "properties": props, "required": required}, cmd)
        return ts

    def status(self) -> dict:
        return {"enabled": self.enabled, "running": self.broker is not None,
                "port": self.broker.port if self.broker else int(self.cfg.get("port", 1883)),
                "broker": self.broker.stats() if self.broker else None, "topics": len(self.values),
                "esphome": self.esphome_devices()}
