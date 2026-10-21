"""HTTPS 용 자체 서명 인증서. superset-init 이 돌릴 때마다 부른다 — 필요할 때만 새로 만든다.

/certs (superset/certs) 에 남기는 것:
  ca.crt, ca.key         자체 CA. 한 번 만들면 그대로 둔다 — PC 마다 신뢰 등록하는 것은 이 ca.crt 다
  server.crt, server.key nginx 가 내주는 서버 인증서. 이름(SUPERSET_TLS_HOSTS)이 바뀌었거나 만료가 가까우면 새로 만든다

SUPERSET_TLS_HOSTS: 접속 주소로 쓸 호스트 이름과 IP, 쉼표 구분. 인증서에 없는 주소로 접속하면 브라우저가 막는다.
"""

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT = Path("/certs")
CA_DAYS = 3650
# 브라우저는 825일이 넘는 서버 인증서를 거절한다
SERVER_DAYS = 825
RENEW_BEFORE = datetime.timedelta(days=30)


def main():
    hosts = []
    for h in os.environ.get("SUPERSET_TLS_HOSTS", "localhost,127.0.0.1").split(","):
        if h.strip():
            hosts.append(h.strip())
    ca_key, ca_cert = _ca()
    if _server_is_current(hosts):
        print(f"서버 인증서가 이미 있습니다: {', '.join(hosts)}")
        return
    _server(ca_key, ca_cert, hosts)
    print(f"서버 인증서를 만들었습니다: {', '.join(hosts)}")


def _ca():
    key_path, cert_path = OUT / "ca.key", OUT / "ca.crt"
    if key_path.exists() and cert_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        return key, x509.load_pem_x509_certificate(cert_path.read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nl2sql Superset local CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write(key_path, _key_pem(key))
    _write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    print("CA 인증서를 만들었습니다: ca.crt")
    return key, cert


def _server_is_current(hosts: list[str]) -> bool:
    cert_path = OUT / "server.crt"
    if not cert_path.exists() or not (OUT / "server.key").exists():
        return False
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    if cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) < RENEW_BEFORE:
        return False
    return sorted(_names_in(cert)) == sorted(hosts)


def _names_in(cert) -> list[str]:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = []
    for name in san.get_values_for_type(x509.DNSName):
        names.append(name)
    for ip in san.get_values_for_type(x509.IPAddress):
        names.append(str(ip))
    return names


def _server(ca_key, ca_cert, hosts: list[str]):
    alt_names = []
    for h in hosts:
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            alt_names.append(x509.DNSName(h))
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=SERVER_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write(OUT / "server.key", _key_pem(key))
    # nginx 는 서버 인증서 뒤에 CA 를 이어 붙인 체인을 내준다
    chain = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    _write(OUT / "server.crt", chain)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _write(path: Path, data: bytes):
    path.write_bytes(data)
    if path.suffix == ".key":
        path.chmod(0o600)


if __name__ == "__main__":
    main()
