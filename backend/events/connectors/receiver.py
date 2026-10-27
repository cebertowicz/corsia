"""A connector's webhook receiver, declared as data on its manifest.

The platform's one handler (``api.webhook_receivers``) serves
``/webhooks/<connector>/`` from these declarations. It enforces the body limit and
verifies the signature before translation. Translators never receive the secret. What happens next,
redelivery dedupe included, belongs to the connector's own admission.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from infra.key_store import BuiltinKey

DEFAULT_BODY_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class ReceiverDelivery:
    """One verified delivery. Header names are lowercase; the signature header and the secret are not here."""

    connector: str
    body: bytes
    headers: Mapping[str, str]
    delivery_id: str


@dataclass(frozen=True)
class ReceiverOutcome:
    """What a translator answers; ``202`` is the default once its events are admitted."""

    status_code: int = 202
    content: dict[str, Any] = field(default_factory=lambda: {"status": "accepted"})


Translator = Callable[[ReceiverDelivery], Awaitable[ReceiverOutcome]]


@dataclass(frozen=True)
class ReceiverDecl:
    """How one connector receives webhooks (event-rules design §5.3, R31)."""

    translator: Translator
    secret: BuiltinKey
    signature_header: str
    signature_prefix: str
    delivery_id_header: str
    max_body_bytes: int = DEFAULT_BODY_LIMIT

    def __post_init__(self) -> None:
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
