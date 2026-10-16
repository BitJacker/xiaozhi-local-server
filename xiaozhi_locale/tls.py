"""HTTPS in rete locale: certificato autofirmato generato automaticamente (oppure il tuo).

Il browser mostrerà un avviso la prima volta ("connessione non privata"): è normale per un certificato
creato dal tuo server e non da un'autorità pubblica. Dopo averlo accettato, password, cookie e voce viaggiano
cifrati e il microfono funziona anche da altri PC e dal telefono.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
import ssl
from pathlib import Path

log = logging.getLogger("tls")


def _make_self_signed(cert_path: Path, key_path: Path, ips: list[str]):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Husky locale ({host})")])
    alt = [x509.DNSName("localhost"), x509.DNSName(host)]
    for ip in sorted(set(ips) | {"127.0.0.1"}):
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Creato il certificato HTTPS per %s", ", ".join(sorted(set(ips))))


def _cert_ips(cert_path: Path) -> set[str]:
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    except x509.ExtensionNotFound:
        return set()


def ssl_context(scfg: dict, data_dir: Path, lan_ip: str) -> ssl.SSLContext:
    if scfg.get("tls_cert") and scfg.get("tls_key"):  # certificato fornito dall'utente
        cert, key = Path(scfg["tls_cert"]), Path(scfg["tls_key"])
    else:
        cert, key = data_dir / "tls" / "cert.pem", data_dir / "tls" / "key.pem"
        # se l'indirizzo del server è cambiato, il certificato viene rifatto
        if not cert.exists() or not key.exists() or lan_ip not in _cert_ips(cert):
            _make_self_signed(cert, key, [lan_ip])
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx
