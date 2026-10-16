"""Test MQTT: broker integrato, account e permessi, sensori, comandi, ESPHome, broker esterni.

Il broker viene provato con paho-mqtt, il client MQTT più diffuso (lo stesso protocollo di ESP8266/ESP32 e ESPHome):
così si verifica che parli MQTT standard e non solo con se stesso. Non serve nessun server acceso.
Uso:  python tests/test_mqtt.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as paho

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale.cryptobox import Box  # noqa: E402
from xiaozhi_locale.custom_tools import CustomTools  # noqa: E402
from xiaozhi_locale.mqtt_hub import MqttHub  # noqa: E402

T = ROOT / "tests" / "_mqtt_test"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


class FakeAuth:
    def device_ok(self, mac, uuid, token):
        return token == "token-giusto"


# il server gira in un suo thread con il suo loop asyncio, come nel programma vero
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
run = lambda coro: asyncio.run_coroutine_threadsafe(coro, loop).result(15)  # noqa: E731


def client(user=None, pw=None, cid=None):
    c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=cid or f"test-{time.monotonic_ns()}")
    if user:
        c.username_pw_set(user, pw)
    got, rc = [], []
    c.on_message = lambda cl, ud, m: got.append((m.topic, m.payload.decode(), m.retain))
    c.on_connect = lambda cl, ud, flags, reason, props: rc.append(reason.value if hasattr(reason, "value") else reason)
    c.got, c.rc = got, rc
    return c


def connect(c, port):
    c.connect("127.0.0.1", port, keepalive=30)
    c.loop_start()
    for _ in range(50):
        if c.rc:
            break
        time.sleep(0.05)
    return c.rc[0] if c.rc else None


def wait(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


def main():
    shutil.rmtree(T, ignore_errors=True)
    T.mkdir(parents=True)
    hub = MqttHub(T, {"port": 0, "host": "127.0.0.1"}, FakeAuth(), Box(T / "k.key"))
    check("All'inizio MQTT è spento", not hub.enabled and hub.broker is None)
    run(hub.set_enabled(True))
    port = hub.broker.port
    check("Acceso dalla pagina Admin: il broker è in ascolto", hub.broker is not None and port > 0, f"porta {port}")

    anon = client()
    rc = connect(anon, port)
    check("Senza account il broker rifiuta", rc not in (0, None), f"codice {rc}")
    anon.loop_stop()

    check("Password troppo corta per un account rifiutata", hub.add_account("sensori", "corta") is not None)
    hub.add_account("sensori", "password-sensori-1")
    hub.add_account("aula2", "password-aula-2", pub=["scuola/aula2/#"], sub=["scuola/aula2/#"])

    sub = client("sensori", "password-sensori-1")
    check("Con l'account giusto si collega (paho-mqtt)", connect(sub, port) == 0)
    sub.subscribe("scuola/#")
    time.sleep(0.3)
    pub = client("sensori", "password-sensori-1")
    connect(pub, port)
    pub.publish("scuola/aula1/temperatura", "22.5")
    check("Un messaggio arriva a chi è iscritto", wait(lambda: ("scuola/aula1/temperatura", "22.5", False) in sub.got), str(sub.got))
    check("Husky ricorda l'ultimo valore", (hub.last_value("builtin", "scuola/aula1/temperatura") or ("",))[0] == "22.5")

    pub.publish("scuola/aula1/umidita", "41", retain=True)
    time.sleep(0.3)
    late = client("sensori", "password-sensori-1")
    connect(late, port)
    late.subscribe("scuola/aula1/umidita")
    check("Messaggi \"retained\": chi si iscrive dopo riceve l'ultimo valore",
          wait(lambda: any(t == "scuola/aula1/umidita" and p == "41" for t, p, _ in late.got)), str(late.got))

    lim = client("aula2", "password-aula-2")
    connect(lim, port)
    lim.publish("scuola/aula1/temperatura", "99")
    lim.publish("scuola/aula2/temperatura", "19")
    time.sleep(0.4)
    check("Permessi: un account può scrivere solo nei suoi argomenti",
          hub.last_value("builtin", "scuola/aula1/temperatura")[0] == "22.5"
          and (hub.last_value("builtin", "scuola/aula2/temperatura") or ("",))[0] == "19")
    lim.subscribe("#")
    lim.subscribe("scuola/aula2/+")
    time.sleep(0.3)
    lim.got.clear()
    pub.publish("scuola/aula1/temperatura", "23")
    pub.publish("scuola/aula2/luce", "on")
    time.sleep(0.4)
    check("Permessi: un account legge solo i suoi argomenti (# rifiutato)",
          [t for t, _, _ in lim.got] == ["scuola/aula2/luce"], str(lim.got))

    # ---------------- strumenti personalizzati di tipo MQTT
    ct = CustomTools(T)
    ct.mqtt = hub
    read = ct.save({"kind": "mqtt", "name": "temperatura_aula1", "description": "x", "action": "read",
                    "topic": "scuola/aula1/temperatura", "roles": ["user"]})
    write = ct.save({"kind": "mqtt", "name": "luce_aula1", "description": "x", "action": "write",
                     "topic": "scuola/aula1/luce/set", "payload": "{stato}", "roles": ["admin"],
                     "params": [{"name": "stato", "type": "string", "enum": ["on", "off"]}]})
    r = run(ct.run(read, {}))
    check("Strumento \"leggi un sensore\": restituisce il valore e quanto è recente", r.startswith("23") and "secondi fa" in r, r)
    sub.got.clear()
    r = run(ct.run(write, {"stato": "on"}))
    check("Strumento \"comanda un dispositivo\": il messaggio arriva al dispositivo",
          wait(lambda: ("scuola/aula1/luce/set", "on", False) in sub.got), f"{r} {sub.got}")

    # ---------------- ESPHome: discovery come lo pubblica un dispositivo vero (abbreviazioni e ~)
    esp = client("sensori", "password-sensori-1", cid="laboratorio")
    connect(esp, port)
    dev = {"ids": "a1b2c3", "name": "laboratorio", "mdl": "esp32dev", "mf": "espressif"}
    esp.publish("homeassistant/sensor/laboratorio/temperatura/config", json.dumps({
        "~": "laboratorio/sensor/temperatura", "name": "Temperatura", "stat_t": "~/state", "unit_of_meas": "°C",
        "dev_cla": "temperature", "uniq_id": "ESPsensortemperatura", "dev": dev}), retain=True)
    esp.publish("homeassistant/switch/laboratorio/ventola/config", json.dumps({
        "~": "laboratorio/switch/ventola", "name": "Ventola", "stat_t": "~/state", "cmd_t": "~/command",
        "uniq_id": "ESPswitchventola", "dev": dev}), retain=True)
    esp.publish("homeassistant/light/laboratorio/led/config", json.dumps({
        "~": "laboratorio/light/led", "name": "LED", "schema": "json", "stat_t": "~/state", "cmd_t": "~/command",
        "brightness": True, "uniq_id": "ESPlightled", "dev": dev}), retain=True)
    esp.publish("laboratorio/sensor/temperatura/state", "24.3", retain=True)
    esp.publish("laboratorio/switch/ventola/state", "OFF", retain=True)
    time.sleep(0.5)
    user_tools = hub.toolset_for("user").names()
    admin_tools = hub.toolset_for("admin").names()
    check("ESPHome: il dispositivo viene riconosciuto da solo e diventa uno strumento di lettura",
          user_tools == ["esphome_laboratorio"], str(user_tools))
    check("ESPHome: i comandi solo per admin e dispositivi",
          "laboratorio_ventola" in admin_tools and "laboratorio_led" in admin_tools and "laboratorio_ventola" not in user_tools,
          str(admin_tools))
    reading = run(hub.toolset_for("user").call("esphome_laboratorio", {}))
    check("ESPHome: lettura dei sensori con unità", "Temperatura: 24.3 °C" in reading, reading)
    esp.subscribe("laboratorio/#")
    time.sleep(0.3)
    esp.got.clear()
    tools = hub.toolset_for("admin")
    run(tools.call("laboratorio_ventola", {"stato": "on"}))
    run(tools.call("laboratorio_led", {"stato": "on", "luminosita": 50}))
    ok_fan = wait(lambda: ("laboratorio/switch/ventola/command", "ON", False) in esp.got
                  and any(t == "laboratorio/light/led/command" for t, _, _ in esp.got))
    led = [json.loads(p) for t, p, _ in esp.got if t == "laboratorio/light/led/command"]
    check("ESPHome: \"accendi la ventola\" e \"luce al 50%\" arrivano al dispositivo nel formato giusto",
          ok_fan and led and led[0] == {"state": "ON", "brightness": 128}, str(esp.got))
    check("ESPHome: elenco per la pagina Admin", hub.esphome_devices()[0]["name"] == "laboratorio"
          and len(hub.esphome_devices()[0]["entities"]) == 3)

    # ---------------- broker esterno (es. The Things Network): qui un secondo broker locale fa da esterno
    ext_dir = T / "ext"
    ext_dir.mkdir()
    other = MqttHub(ext_dir, {"port": 0, "host": "127.0.0.1"}, FakeAuth())
    run(other.set_enabled(True))
    other.add_account("ttn", "password-ttn-lunga")
    err = run(hub.save_external({"name": "TTN di prova", "host": "127.0.0.1", "port": other.broker.port,
                                 "username": "ttn", "password": "password-ttn-lunga", "topics": ["v3/+/devices/+/up"]}))
    ext_id = hub.store.data["external"][0]["id"]
    check("Broker esterno salvato (password cifrata, mai rimandata al browser)",
          err is None and "password-ttn" not in (T / "mqtt.json").read_text(encoding="utf-8")
          and "password" not in hub.external_list()[0], str(err))
    check("Broker esterno: collegato", wait(lambda: hub.external_list()[0]["connected"], 5), hub.external_list()[0].get("error"))
    run(other.broker.publish("v3/scuola@ttn/devices/sensore-1/up", '{"decoded_payload":{"temp":18.5}}'))
    check("LoRaWAN: il dato del sensore arriva a Husky dal broker esterno",
          wait(lambda: (hub.last_value(ext_id, "v3/scuola@ttn/devices/sensore-1/up") or ("",))[0].startswith('{"decoded')))
    check("Privacy: il broker esterno compare nella pagina Admin", hub.external_hosts()[0]["url"].startswith("127.0.0.1:"))
    lora = ct.save({"kind": "mqtt", "name": "temperatura_esterna", "description": "x", "action": "read", "broker": ext_id,
                    "topic": "v3/scuola@ttn/devices/sensore-1/up", "extract": "decoded_payload.temp", "roles": ["user"]})
    r = run(ct.run(lora, {}))
    check("LoRaWAN: lo strumento legge solo il campo indicato dal messaggio JSON", r.startswith("18.5 "), r)

    # ---------------- dispositivi xiaozhi: entrano solo col loro token
    bad = client("x", "token-sbagliato", cid="husky@@@aa_bb_cc_dd_ee_ff@@@uuid-1")
    good = client("x", "token-giusto", cid="husky@@@aa_bb_cc_dd_ee_ff@@@uuid-1")
    check("Dispositivo xiaozhi con token sbagliato rifiutato", connect(bad, port) not in (0, None))
    check("Dispositivo xiaozhi approvato accettato", connect(good, port) == 0)

    # ---------------- revoca e spegnimento
    run(hub.remove_account("aula2"))
    check("Account eliminato: chi era collegato viene scollegato", wait(lambda: not lim.is_connected()))
    run(hub.set_enabled(False))
    check("Spento dalla pagina Admin: il broker smette di ascoltare", hub.broker is None and wait(lambda: not sub.is_connected()))
    for c in (sub, pub, late, lim, esp, good, bad):
        c.loop_stop()
    run(other.stop())
    run(hub.stop())
    shutil.rmtree(T, ignore_errors=True)
    print(f"\n==== {sum(RESULTS)}/{len(RESULTS)} test MQTT superati ====")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
