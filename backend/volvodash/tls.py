"""Serve https without a reverse proxy, using a certificate the dashboard makes itself.

On by default: Volvo ID's new sign-in service only accepts https redirect
addresses. VOLVODASH_TLS=off serves plain http (behind a proxy that does https,
say). The certificate lives in the data folder
and is renewed a month before it runs out. Browsers warn about it once, since
nobody vouches for it; a real certificate can be used instead with
VOLVODASH_TLS_CERT and VOLVODASH_TLS_KEY.
"""

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path

# Browsers refuse certificates that last longer than this.
DAYS = 397
RENEW_DAYS = 30


def files(data: Path) -> tuple[str, str] | None:
    """The certificate and key to serve https with, or None for plain http."""
    cert, key = os.environ.get("VOLVODASH_TLS_CERT"), os.environ.get("VOLVODASH_TLS_KEY")
    if cert and key:
        return cert, key
    if not enabled():
        return None
    folder = data / "tls"
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    names = _names()
    if not _still_good(cert_path, key_path, names):
        folder.mkdir(parents=True, exist_ok=True)
        make(cert_path, key_path, names)
    return str(cert_path), str(key_path)


def enabled() -> bool:
    if os.environ.get("VOLVODASH_TLS_CERT"):
        return True
    return os.environ.get("VOLVODASH_TLS", "auto").lower() not in ("off", "0", "false", "no")


def _names() -> list[str]:
    """Names the certificate covers: localhost, this machine, and VOLVODASH_TLS_HOSTS."""
    extra = [h.strip() for h in os.environ.get("VOLVODASH_TLS_HOSTS", "").split(",") if h.strip()]
    return list(dict.fromkeys(["localhost", "127.0.0.1", socket.gethostname(), *extra]))


def _still_good(cert_path: Path, key_path: Path, names: list[str]) -> bool:
    from cryptography import x509

    if not cert_path.exists() or not key_path.exists():
        return False
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (ValueError, x509.ExtensionNotFound):
        return False
    covered = {str(n) for n in san.get_values_for_type(x509.DNSName)} | {
        str(n) for n in san.get_values_for_type(x509.IPAddress)}
    left = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
    return set(names) <= covered and left > dt.timedelta(days=RENEW_DAYS)


def make(cert_path: Path, key_path: Path, names: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    for n in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            alt.append(x509.DNSName(n))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Volvo Dash")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DAYS))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
