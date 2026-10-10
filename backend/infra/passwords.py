"""Password hashing — scrypt, with the parameters stored beside the digest.

Two properties are load-bearing.

**The cost parameters live in the encoded string**, not in this module's
constants. A stored hash is verified with the parameters it was created with,
so raising the cost later keeps every existing password verifiable instead of
locking its owner out. New hashes use the constants below.

**Comparison is constant-time.** A byte-by-byte ``==`` on the digest leaks how
much of it matched through timing, which is a distinguisher an attacker can
grind against. ``hmac.compare_digest`` does not.

The cost is deliberate: one verification allocates ~128 MB and takes ~0.3 s by
design, which is what makes an offline guess expensive. Because the backend
binds ``0.0.0.0``, that cost is also a denial-of-service surface, so the login
route serializes hashing rather than letting concurrent attempts multiply it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: OWASP's scrypt minimum (2024): N=2^17, r=8, p=1.
_N = 1 << 17
_R = 8
_P = 1
_SALT_BYTES = 16
_DK_LEN = 32
_SCHEME = "scrypt"

#: Below this a password is not worth the hash. Enforced where a password is
#: chosen (CLI, API), not where one is verified.
MIN_PASSWORD_LENGTH = 8


class PasswordFormatError(ValueError):
    """A stored password hash is not in a format this build can verify."""


def _b64(raw: bytes) -> str:
    """Encode bytes as unpadded base64, safe inside a ``$``-delimited field."""
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """Decode the unpadded base64 produced by :func:`_b64`."""
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    """Run scrypt with explicit parameters and a memory ceiling that fits them."""
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=128 * n * r * p + (1 << 20),
        dklen=_DK_LEN,
    )


def hash_password(password: str) -> str:
    """Return ``scrypt$N$r$p$salt$digest`` for ``password``, with a fresh salt."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _N, _R, _P)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """True iff ``password`` reproduces the digest in ``encoded``.

    Raises :class:`PasswordFormatError` when ``encoded`` is not a hash this
    build understands — an unverifiable stored hash is an operator problem to
    see, not a wrong password to report.
    """
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
    except (AttributeError, ValueError) as error:
        raise PasswordFormatError("stored password hash is malformed") from error
    if scheme != _SCHEME:
        raise PasswordFormatError(f"unsupported password hash scheme: {scheme!r}")
    try:
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt, expected = _unb64(salt_text), _unb64(digest_text)
    except (ValueError, base64.binascii.Error) as error:  # type: ignore[attr-defined]
        raise PasswordFormatError("stored password hash is malformed") from error
    if not password:
        return False
    return hmac.compare_digest(_derive(password, salt, n, r, p), expected)
