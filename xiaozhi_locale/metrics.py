"""Metriche per Prometheus/Grafana su /metrics (formato testo di Prometheus).

Spente di serie: si accendono dalla pagina Admin (Impostazioni), che mostra anche il token da mettere nella
configurazione di Prometheus (bearer_token). Contengono solo numeri (quante domande, tempi di risposta,
connessioni, dispositivi): MAI il testo delle conversazioni, nomi di utenti o indirizzi.
"""
from __future__ import annotations

import time

from . import config as config_mod

BUCKETS = (0.25, 0.5, 1, 1.5, 2, 3, 5, 8, 13, 20)


class Histogram:
    def __init__(self):
        self.counts = [0] * (len(BUCKETS) + 1)
        self.sum = 0.0
        self.n = 0

    def observe(self, v: float):
        self.n += 1
        self.sum += v
        for i, b in enumerate(BUCKETS):
            if v <= b:
                self.counts[i] += 1
                return
        self.counts[-1] += 1

    def lines(self, name: str, labels: str = "") -> list[str]:
        out, acc = [], 0
        sep = "," if labels else ""
        for b, c in zip(BUCKETS, self.counts):
            acc += c
            out.append(f'{name}_bucket{{{labels}{sep}le="{b}"}} {acc}')
        out.append(f'{name}_bucket{{{labels}{sep}le="+Inf"}} {self.n}')
        out.append(f"{name}_sum{{{labels}}} {round(self.sum, 3)}" if labels else f"{name}_sum {round(self.sum, 3)}")
        out.append(f"{name}_count{{{labels}}} {self.n}" if labels else f"{name}_count {self.n}")
        return out


class Metrics:
    def __init__(self):
        self.started = time.time()
        self.turns = {"voice": 0, "text": 0}
        self.first_audio = {"voice": Histogram(), "text": Histogram()}
        self.asr = Histogram()
        self.llm_first = Histogram()
        self.errors = 0

    def record_turn(self, t: dict):
        kind = "voice" if t.get("voice") else "text"
        self.turns[kind] += 1
        if t.get("first_audio") is not None:
            self.first_audio[kind].observe(t["first_audio"])
        if t.get("asr") is not None:
            self.asr.observe(t["asr"])
        if t.get("llm_first") is not None:
            self.llm_first.observe(t["llm_first"])

    def render(self, app) -> str:
        L: list[str] = []

        def metric(name, kind, help_, *values):
            L.append(f"# HELP {name} {help_}")
            L.append(f"# TYPE {name} {kind}")
            L.extend(values)
        metric("husky_info", "gauge", "Versione del server.", f'husky_info{{version="{config_mod.version()}"}} 1')
        metric("husky_uptime_seconds", "gauge", "Secondi dall'avvio.", f"husky_uptime_seconds {round(time.time() - self.started)}")
        metric("husky_turns_total", "counter", "Domande ricevute.",
               *(f'husky_turns_total{{kind="{k}"}} {v}' for k, v in self.turns.items()))
        metric("husky_first_audio_seconds", "histogram", "Dalla fine della domanda al primo audio della risposta.",
               *(line for k, h in self.first_audio.items() for line in h.lines("husky_first_audio_seconds", f'kind="{k}"')))
        metric("husky_asr_seconds", "histogram", "Tempo del riconoscimento vocale.", *self.asr.lines("husky_asr_seconds"))
        metric("husky_llm_first_token_seconds", "histogram", "Attesa della prima parola del modello linguistico.",
               *self.llm_first.lines("husky_llm_first_token_seconds"))
        sessions = list(app["active_sessions"])
        web = sum(1 for s in sessions if s.device_id == "web")
        metric("husky_connections", "gauge", "Conversazioni aperte.",
               f'husky_connections{{kind="web"}} {web}', f'husky_connections{{kind="device"}} {len(sessions) - web}')
        metric("husky_speaking", "gauge", "Conversazioni che stanno rispondendo adesso.",
               f"husky_speaking {sum(1 for s in sessions if s.speaking)}")
        auth = app["auth"]
        approved = sum(1 for d in auth.devices.data.values() if d.get("status") == "approved") if auth.enabled else 0
        metric("husky_devices_approved", "gauge", "Dispositivi xiaozhi approvati.", f"husky_devices_approved {approved}")
        metric("husky_users", "gauge", "Account.", f"husky_users {len(auth.users.data) if auth.enabled else 0}")
        esp = app.get("esphome")
        metric("husky_esphome_connected", "gauge", "Dispositivi ESPHome collegati.",
               f"husky_esphome_connected {len(esp.online()) if esp else 0}")
        mq = app.get("mqtt")
        clients = len(mq.broker.clients) if mq is not None and mq.broker is not None else 0
        metric("husky_mqtt_clients", "gauge", "Client collegati al broker MQTT.", f"husky_mqtt_clients {clients}")
        rooms = app.get("rooms")
        if rooms is not None:
            metric("husky_room_silenced_total", "counter", "Risposte evitate perché ha risposto un altro dispositivo dell'aula.",
                   f"husky_room_silenced_total {rooms.stats['silenced']}")
        ev = app["events"]
        metric("husky_login_failed_total", "counter", "Accessi falliti dall'avvio.",
               f"husky_login_failed_total {ev.counts.get('login_failed', 0)}")
        backup = app.get("backup")
        if backup is not None:
            metric("husky_backup_last_success_timestamp", "gauge", "Ora dell'ultimo backup riuscito (Unix).",
                   f"husky_backup_last_success_timestamp {int(backup.last_ok or 0)}")
        try:
            import resource  # solo Linux/macOS
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        except ImportError:
            rss = _windows_rss()
        if rss:
            metric("husky_process_max_resident_bytes", "gauge", "Memoria massima usata dal server.",
                   f"husky_process_max_resident_bytes {rss}")
        return "\n".join(L) + "\n"


def _windows_rss() -> int:
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return int(pmc.PeakWorkingSetSize)
    except Exception:
        pass
    return 0
