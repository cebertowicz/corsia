"""Stable TLS identity, certificate storage, and signed proofs for devices."""

from __future__ import annotations

import ipaddress
import os
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from infra.config import STATE_ROOT


def _key_paths(state_root: Path = STATE_ROOT) -> tuple[Path, Path]:
    folder = state_root / "devices"
    return folder / "host-key.pem", folder / "host-cert.pem"


def _secure_file(path: Path) -> None:
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
        raise RuntimeError("Devices host key files have unsafe ownership or permissions")


def _certificate_for_key(key) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CORSIA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _private_key_bytes(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def ensure_host_key(state_root: Path = STATE_ROOT) -> tuple[Path, Path]:
    """Load, or securely repair, the host's stable ECDSA identity."""
    key_path, cert_path = _key_paths(state_root)
    folder = key_path.parent
    key_exists = key_path.exists()
    cert_exists = cert_path.exists()
    if key_exists:
        _secure_file(key_path)
    if cert_exists:
        _secure_file(cert_path)
    if key_exists and cert_exists:
        return key_path, cert_path

    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    if key_exists:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        _write_private_file(cert_path, _certificate_for_key(key))
        return key_path, cert_path

    key = ec.generate_private_key(ec.SECP256R1())
    _write_private_file(key_path, _private_key_bytes(key))
    _write_private_file(cert_path, _certificate_for_key(key))
    return key_path, cert_path


def host_spki_der(state_root: Path = STATE_ROOT) -> bytes:
    """Return the stable host public key as SubjectPublicKeyInfo DER."""
    _, cert_path = ensure_host_key(state_root)
    return _parsed(cert_path, _spki_der)


def host_spki_sha256(state_root: Path = STATE_ROOT) -> bytes:
    """Return the SHA-256 binding for the listener's stable public key."""
    spki = host_spki_der(state_root)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return digest.finalize()


#: Parsed host identity files by path, with each file's mtime, so the public
#: /identity route answers without parsing them on every call.
_parsed_files: dict[Path, tuple[int, object]] = {}


def _parsed(path: Path, parse):
    """Return ``parse(bytes)`` for ``path``, re-reading it only when the file changed."""
    modified = path.stat().st_mtime_ns
    cached = _parsed_files.get(path)
    if cached is not None and cached[0] == modified:
        return cached[1]
    value = parse(path.read_bytes())
    _parsed_files[path] = (modified, value)
    return value


def _spki_der(cert_pem: bytes) -> bytes:
    return x509.load_pem_x509_certificate(cert_pem).public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _private_key(key_pem: bytes):
    return serialization.load_pem_private_key(key_pem, password=None)


def sign_identity_proof(nonce: bytes, authority: str, state_root: Path = STATE_ROOT, *, tls_spki_sha256: bytes | None = None) -> bytes:
    """Sign one caller nonce, authority, and the host TLS certificate's SPKI hash."""
    key_path, _ = ensure_host_key(state_root)
    tls_spki_sha256 = host_spki_sha256(state_root) if tls_spki_sha256 is None else tls_spki_sha256
    key = _parsed(key_path, _private_key)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        # The desktop app checks proofs as P-256 ECDSA only; any other saved key cannot prove this host.
        raise ValueError("the host key is not a P-256 key")
    payload = b"corsia-identity-v1\0" + nonce + b"\0" + authority.encode("ascii") + b"\0" + tls_spki_sha256
    return key.sign(payload, ec.ECDSA(hashes.SHA256()))


def _write_private_file(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
