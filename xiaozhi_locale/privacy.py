"""Controllo privacy: gli indirizzi a cui il server si collega devono essere su questo computer o in rete locale.

Il server si connette solo a:
  * llm.base_url        (il modello linguistico, normalmente Ollama su 127.0.0.1)
  * mcp_servers[].url   (es. la HuskyLens in rete locale)
Se uno di questi punta a Internet, all'avvio il server si ferma (a meno di privacy.allow_external: true).
Gli aggiornamenti (GitHub) sono a parte: partono solo dalla pagina Admin o da update.sh / update.ps1.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def host_is_local(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:  # nome (es. nas.local): conta l'indirizzo a cui corrisponde
            ips = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)}
        except OSError:
            return False
    return bool(ips) and all(ip.is_loopback or ip.is_private or ip.is_link_local for ip in ips)


def check(cfg: dict) -> list[dict]:
    """Ritorna l'elenco delle connessioni configurate, ciascuna con local=True/False."""
    targets = [("Modello linguistico (llm.base_url)", (cfg.get("llm") or {}).get("base_url", ""))]
    for m in cfg.get("mcp_servers") or []:
        targets.append((f"Server MCP '{m.get('name')}'", m.get("url", "")))
    out = []
    for what, url in targets:
        host = urlparse(url).hostname or ""
        out.append({"what": what, "url": url, "host": host, "local": host_is_local(host)})
    return out
