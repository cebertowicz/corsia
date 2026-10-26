"""A connector's webhook receiver, declared as data on its manifest.

The platform's one handler (``api.webhook_receivers``) serves
``/webhooks/<connector>/`` from these declarations. It enforces the body limit and
verifies the signature itself, before any connector code runs, so a translator only
ever sees a verified delivery and never holds the secret. What happens next, redelivery
dedupe included, belongs to the connector's own admission.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The signature schemes the platform can verify. A tool that needs another gets it added here.
SIGNATURE_SCHEMES: tuple[str, ...] = ("hmac_sha256",)
DEFAULT_BODY_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class ReceiverDelivery:
    """One verified delivery. Header names are lowercase; the signature header and the secret are not here."""

    connector: str
    subpath: str
    body: bytes
    headers: Mapping[str, str]
    delivery_id: str


@dataclass(frozen=True)
class ReceiverOutcome:
    """What a translator answers; ``202`` is the default once its events are admitted."""

    status_code: int = 202
    content: dict[str, Any] = field(default_factory=lambda: {"status": "accepted"})


Translator = Callable[[ReceiverDelivery], Awaitable[ReceiverOutcome | None]]


@dataclass(frozen=True)
class ReceiverDecl:
    """How one connector receives webhooks (event-rules design §5.3, R31)."""

    translator: Translator
    signature_header: str
    signature_prefix: str
    delivery_id_header: str
    signature_scheme: str = SIGNATURE_SCHEMES[0]
    max_body_bytes: int = DEFAULT_BODY_LIMIT
    #: A health check the platform answers without a secret, only while none is configured.
    open_probe: Callable[[Mapping[str, str]], bool] | None = None

    def __post_init__(self) -> None:
        if self.signature_scheme not in SIGNATURE_SCHEMES:
            raise ValueError(f"unknown signature scheme {self.signature_scheme!r}")
        if self.max_body_bytes <= 0:
            raise ValueError("a receiver's body limit must be positive")
        for name in (self.signature_header, self.delivery_id_header):
            if name != name.lower():
                raise ValueError(f"declare header names in lowercase: {name!r}")


def verify_hmac_sha256(body: bytes, header_value: str, prefix: str, secret: str) -> bool:
    """Whether ``header_value`` is ``prefix`` plus the hex HMAC-SHA256 of ``body`` under ``secret``."""
    if not header_value.startswith(prefix):
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), header_value[len(prefix):])


def verify_signature(decl: ReceiverDecl, body: bytes, header_value: str, secret: str) -> bool:
    """Verify with the declared scheme; the only place a scheme is chosen."""
    return verify_hmac_sha256(body, header_value, decl.signature_prefix, secret)
