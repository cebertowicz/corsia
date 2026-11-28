"""Web Push encryption (RFC 8291, aes128gcm of RFC 8188) and the VAPID ES256 token (RFC 8292), on `cryptography` only.

Design prototype for R8: nothing here calls the network. `encrypt` takes the subscription's public key and auth secret and
returns the request body; `vapid_token` returns the signed JWT. Both are small enough to check against the RFC's own values.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_SIZE = 4096
#: The ES256 token is valid for 12 hours, inside RFC 8292's 24-hour cap.
VAPID_LIFETIME_SECONDS = 12 * 60 * 60


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = "".join(text.split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def private_from_bytes(raw: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *, as_private: ec.EllipticCurvePrivateKey | None = None,
            salt: bytes | None = None) -> bytes:
    """The aes128gcm body for one push: the 86-octet header, then one record. `as_private` and `salt` are random unless a test fixes them."""
    if len(plaintext) > RECORD_SIZE - 17 - 1:
        raise ValueError("payload does not fit one record")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = public_bytes(as_private)
    shared = as_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hmac(_hmac(auth_secret, shared), b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    header = salt + struct.pack(">IB", RECORD_SIZE, len(as_public)) + as_public
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def vapid_token(audience: str, subject: str, private: ec.EllipticCurvePrivateKey, *, now: float | None = None) -> str:
    """The VAPID JWT (ES256): audience is the push service's origin, subject the one fixed `sub` of every install."""
    now = time.time() if now is None else now
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps({"aud": audience, "exp": int(now) + VAPID_LIFETIME_SECONDS, "sub": subject}, separators=(",", ":")).encode())
    der = private.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def vapid_public(private: ec.EllipticCurvePrivateKey) -> str:
    """The application server key the browser subscribes with (`applicationServerKey`), base64url."""
    return b64u(public_bytes(private))
