"""The certificate the dashboard makes for itself when serving https."""

import datetime as dt

from cryptography import x509

from volvodash import tls


def test_on_unless_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv("VOLVODASH_TLS_CERT", raising=False)
    monkeypatch.delenv("VOLVODASH_TLS", raising=False)
    assert tls.enabled()
    monkeypatch.setenv("VOLVODASH_TLS", "off")
    assert tls.files(tmp_path) is None


def test_makes_keeps_and_renews_its_certificate(tmp_path, monkeypatch):
    monkeypatch.delenv("VOLVODASH_TLS_CERT", raising=False)
    monkeypatch.setenv("VOLVODASH_TLS", "auto")
    monkeypatch.setenv("VOLVODASH_TLS_HOSTS", "192.168.2.12, volvo.example.net")
    cert_path, key_path = tls.files(tmp_path)
    cert = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "volvo.example.net" in san.get_values_for_type(x509.DNSName)
    assert "192.168.2.12" in [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    assert cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) <= dt.timedelta(days=398)
    assert (tmp_path / "tls" / "key.pem").stat().st_mode & 0o077 == 0

    # Restarting keeps it, so the browser's exception still holds.
    first = open(cert_path, "rb").read()
    tls.files(tmp_path)
    assert open(cert_path, "rb").read() == first

    # A new address means a new certificate that covers it.
    monkeypatch.setenv("VOLVODASH_TLS_HOSTS", "192.168.2.12, volvo.example.net, 10.0.0.5")
    tls.files(tmp_path)
    assert open(cert_path, "rb").read() != first
