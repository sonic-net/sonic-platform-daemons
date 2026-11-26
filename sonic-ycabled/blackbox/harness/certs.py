"""Certificates for the TLS cases: a CA, a SoC server certificate and a
ToR client certificate."""

import datetime
import ipaddress
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SOC_NAME = "soc.bb.test"


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert(subject, issuer, key, issuer_key, ca=False, sans=(), usage=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
               .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(days=1))
               .not_valid_after(now + datetime.timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
    if usage:
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


class Pki(object):
    """Writes the files to `directory` and keeps the PEM bytes."""

    def __init__(self, directory, soc_ips=()):
        os.makedirs(directory, exist_ok=True)
        ca_key = _key()
        self.ca = _cert("bb-ca", "bb-ca", ca_key, ca_key, ca=True)
        server_key = _key()
        sans = [x509.DNSName(SOC_NAME)] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in soc_ips]
        self.server_cert = _cert(SOC_NAME, "bb-ca", server_key, ca_key, sans=sans,
                                 usage=ExtendedKeyUsageOID.SERVER_AUTH)
        self.server_key = _pem_key(server_key)
        client_key = _key()
        self.client_cert = _cert("bb-tor", "bb-ca", client_key, ca_key, usage=ExtendedKeyUsageOID.CLIENT_AUTH)
        self.client_key = _pem_key(client_key)
        other_ca_key = _key()
        self.other_ca = _cert("bb-other-ca", "bb-other-ca", other_ca_key, other_ca_key, ca=True)

        self.paths = {}
        for name, data in (("ca.crt", self.ca), ("client.crt", self.client_cert),
                           ("client.key", self.client_key), ("other-ca.crt", self.other_ca)):
            path = os.path.join(directory, name)
            with open(path, "wb") as f:
                f.write(data)
            self.paths[name] = path

    def server_tls(self, require_client_auth=False):
        return {"cert": self.server_cert, "key": self.server_key, "ca": self.ca,
                "require_client_auth": require_client_auth}
