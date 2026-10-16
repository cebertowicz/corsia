"""In-memory state machine for a host's short-lived pairing code."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 4
CODE_TTL_SECONDS = 10 * 60
MAX_WRONG_TRIES = 5
MAX_HOURLY_FAILURES = 20
MAX_PENDING_EXCHANGES = 4
EXCHANGE_TTL_SECONDS = 60
PAIRING_IDENTITY = b"corsia-pair-v1"


class PairingError(Exception):
    """A stable, public pairing failure reason."""

    def __init__(self, reason: Literal["expired", "wrong_code", "too_many_tries", "too_many_pending", "paused", "reflected", "invalid_exchange"]):
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _Exchange:
    generation: int
    expires_at: float
    msg_c: bytes
    msg_s: bytes
    spake: object
    transcript_prefix: bytes


class DevicePairing:
    """Own the code, PAKE exchanges and rate limits for one backend process.

    No pairing secret is persisted. Replacing, expiring, consuming, or
    disabling a code invalidates every exchange created from it.
    """

    def __init__(self, *, clock=time.monotonic, on_update: Callable[[], None] | None = None):
        self._clock = clock
        self._on_update = on_update or (lambda: None)
        self._enabled = False
        self._code: str | None = None
        self._pairing_secret: bytes | None = None
        self._expires_at: float | None = None
        self._generation = 0
        self._wrong_tries = 0
        self._failures: deque[float] = deque()
        self._paused = False
        self._exchanges: dict[str, _Exchange] = {}
        self._issued_server_messages: set[bytes] = set()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False
        self._end_code()
        self._on_update()

    def status(self, *, include_code: bool = False) -> dict:
        self._expire_if_needed()
        state = "paused" if self._paused else "active" if self._code else "none"
        result = {"state": state, "expires_at": self._expires_at if state == "active" else None}
        if self._expires_at is not None:
            result["expires_in_seconds"] = max(0, math.ceil(self._expires_at - self._clock()))
        if include_code and self._code is not None:
            result["value"] = self._code
        return result

    def issue_code(self) -> str:
        if not self._enabled:
            raise PairingError("expired")
        self._trim_failures()
        # The feature-owner ruling keeps the rolling hourly window across
        # ordinary code replacement. Only a deliberate New code from paused
        # releases the pause and starts a fresh hourly window.
        if self._paused:
            self._failures.clear()
            self._paused = False
        self._generation += 1
        self._exchanges.clear()
        self._issued_server_messages.clear()
        self._wrong_tries = 0
        self._code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._pairing_secret = hashlib.sha256(
            PAIRING_IDENTITY + b"\0" + self._code.encode()
        ).digest()
        self._expires_at = self._clock() + CODE_TTL_SECONDS
        self._on_update()
        return self._code

    def require_active(self) -> None:
        """Reject work while pairing is disabled, expired, or paused."""
        self._require_active()

    def start(
        self,
        *,
        msg_c: bytes,
        transcript_prefix: bytes,
        spake: object | None = None,
        msg_s: bytes | None = None,
    ) -> tuple[str, bytes]:
        self.require_active()
        self._prune_expired_exchanges()
        if len(self._exchanges) >= MAX_PENDING_EXCHANGES:
            raise PairingError("too_many_pending")
        if msg_c in self._issued_server_messages:
            self._record_failure()
            raise PairingError("reflected")
        if spake is None:
            spake, msg_s = self.start_spake()
        assert msg_s is not None
        exchange_id = secrets.token_urlsafe(24)
        self._exchanges[exchange_id] = _Exchange(
            generation=self._generation,
            expires_at=self._clock() + EXCHANGE_TTL_SECONDS,
            msg_c=msg_c,
            msg_s=msg_s,
            spake=spake,
            transcript_prefix=transcript_prefix,
        )
        self._issued_server_messages.add(msg_s)
        return exchange_id, msg_s

    def start_spake(self) -> tuple[object, bytes]:
        """Do only the expensive SPAKE2 calculation after checking the active code."""
        self.require_active()
        from spake2 import SPAKE2_Symmetric

        assert self._pairing_secret is not None
        spake = SPAKE2_Symmetric(self._pairing_secret, idSymmetric=PAIRING_IDENTITY)
        return spake, spake.start()

    def _prune_expired_exchanges(self) -> None:
        now = self._clock()
        self._exchanges = {
            exchange_id: exchange
            for exchange_id, exchange in self._exchanges.items()
            if exchange.expires_at > now
        }

    def finish(self, *, exchange_id: str, confirm_c: bytes, name: str) -> tuple[bytes, str]:
        self._require_active()
        exchange = self._exchanges.pop(exchange_id, None)
        if exchange is None or exchange.generation != self._generation or exchange.expires_at <= self._clock():
            self._record_failure()
            raise PairingError("invalid_exchange")
        key = exchange.spake.finish(exchange.msg_c)
        client_key, server_key = _split_key(key)
        transcript = exchange.transcript_prefix + exchange.msg_c + exchange.msg_s
        expected = hmac.new(client_key, transcript, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, confirm_c):
            self._record_failure()
            raise PairingError(self._failure_reason())
        normalized_name = pairing_computer_name(name)
        self._end_code()
        self._on_update()
        return (
            hmac.new(
                server_key,
                transcript + b"name:" + normalized_name.encode("utf-8"),
                hashlib.sha256,
            ).digest(),
            normalized_name,
        )

    def _require_active(self) -> None:
        self._expire_if_needed()
        if self._paused:
            raise PairingError("paused")
        if not self._enabled or self._code is None:
            raise PairingError("expired")

    def _expire_if_needed(self) -> None:
        if self._expires_at is not None and self._expires_at <= self._clock():
            self._end_code()
            self._on_update()

    def _end_code(self) -> None:
        self._code = None
        self._pairing_secret = None
        self._expires_at = None
        self._exchanges.clear()
        self._issued_server_messages.clear()

    def _trim_failures(self) -> None:
        now = self._clock()
        while self._failures and self._failures[0] <= now - 3600:
            self._failures.popleft()

    def _record_failure(self) -> None:
        now = self._clock()
        self._failures.append(now)
        self._trim_failures()
        self._wrong_tries += 1
        if len(self._failures) >= MAX_HOURLY_FAILURES:
            self._paused = True
            self._end_code()
            self._on_update()
        elif self._wrong_tries >= MAX_WRONG_TRIES:
            self._end_code()
            self._on_update()

    def _failure_reason(self) -> Literal["wrong_code", "too_many_tries", "paused"]:
        if self._paused:
            return "paused"
        if self._code is None:
            return "too_many_tries"
        return "wrong_code"


def pairing_computer_name(name: str) -> str:
    """Normalize the DNS-SD computer name and bound it to the pairing protocol."""
    normalized = unicodedata.normalize("NFC", name)
    characters: list[str] = []
    byte_count = 0
    for character in normalized:
        encoded = character.encode("utf-8")
        if byte_count + len(encoded) > 63:
            break
        characters.append(character)
        byte_count += len(encoded)
    return "".join(characters)


def canonical_verified_host(host: object) -> str:
    """Canonicalize the verified-mode host exactly once at the protocol seam."""
    if not isinstance(host, str):
        raise ValueError("host must be a string")
    value = host.rstrip(".").lower()
    if not value or ":" in value:
        raise ValueError("host must be a hostname without a port")
    return value


def transcript_prefix(mode: object, *, host: object = None, spki_sha256: bytes | None = None) -> bytes:
    """Return the fixed portion of the mode-specific SPAKE2 transcript."""
    if mode == "verified":
        return PAIRING_IDENTITY + b"verified:" + canonical_verified_host(host).encode() + b"\0"
    if mode == "pinned" and isinstance(spki_sha256, bytes) and len(spki_sha256) == 32:
        return PAIRING_IDENTITY + b"pinned:" + spki_sha256 + b"\0"
    raise ValueError("invalid pairing mode or binding")


def _split_key(key: bytes) -> tuple[bytes, bytes]:
    """Derive independent confirmation keys from a PAKE shared key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    material = HKDF(algorithm=hashes.SHA256(), length=64, salt=None, info=PAIRING_IDENTITY).derive(key)
    return material[:32], material[32:]
