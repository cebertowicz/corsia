"""Urgent push guards (design prototype): what a push may say, where a tap opens, where the install may send.

R19: title at most 40 characters, one line at most 120, control characters stripped.
R20: a tap opens only an Activity item route on the same origin.
R21: sends go only to https endpoints on the known push-service hosts; private, loopback and link-local
     addresses are refused; no redirects; 10-second timeout.
R22: a 404, 410, 400 or 403 answer turns that device Off.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import unicodedata
from collections.abc import Callable
from urllib.parse import urlsplit

TITLE_MAX = 40
LINE_MAX = 120
REASON_MAX = 400
SEND_TIMEOUT_SECONDS = 10.0
#: An undelivered push expires after 24 hours (R19).
TTL_SECONDS = 24 * 60 * 60

#: The known push services: Apple, Google, Mozilla, Microsoft. A host matches when it is one of these or a
#: subdomain of one, on a label boundary.
PUSH_SERVICE_HOSTS = (
    "push.apple.com",
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)
#: R22: the answers that mean this device can no longer receive.
GONE_STATUSES = frozenset({400, 403, 404, 410})

_ACTIVITY_TAP = re.compile(r"^/\?activity=[A-Za-z0-9._~-]{1,100}$")


class PushRefused(ValueError):
    """The text, path or endpoint broke a guard; the message says which."""


def clean(text: object, limit: int, what: str) -> str:
    """Strip control characters, collapse whitespace; refuse a non-string, an empty or an over-long value."""
    if not isinstance(text, str):
        raise PushRefused(f"The {what} must be text.")
    stripped = " ".join("".join(" " if ch.isspace() else ("" if unicodedata.category(ch).startswith("C") else ch) for ch in text).split())
    if not stripped:
        raise PushRefused(f"The {what} is empty.")
    if len(stripped) > limit:
        raise PushRefused(f"The {what} is {len(stripped)} characters; the limit is {limit}.")
    return stripped


def tap_path(item_id: object) -> str:
    """The one path a push opens, built from an Activity item id, never from caller text."""
    if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{1,100}", item_id):
        raise PushRefused("The item_id must be an Activity item id.")
    return f"/?activity={item_id}"


def valid_tap_path(path: object) -> bool:
    """R20: a single leading `/` and an Activity item route; nothing a browser could read as another site."""
    return isinstance(path, str) and bool(_ACTIVITY_TAP.fullmatch(path))


def _host_known(host: str) -> bool:
    return any(host == base or host.endswith("." + base) for base in PUSH_SERVICE_HOSTS)


def _unsafe_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_multicast


def resolve_system(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def validate_endpoint(url: object, resolve: Callable[[str], list[str]] = resolve_system) -> str:
    """R21: return the endpoint when it may be sent to, else raise PushRefused saying why."""
    refusal = "That push address isn't on a known push service."
    if not isinstance(url, str) or len(url) > 2048 or any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise PushRefused(refusal)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.fragment:
        raise PushRefused(refusal)
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or not host.isascii() or parts.port not in (None, 443):
        raise PushRefused(refusal)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PushRefused(refusal)  # an address, not a name
    if not _host_known(host):
        raise PushRefused(refusal)
    try:
        addresses = resolve(host)
    except OSError:
        raise PushRefused(refusal) from None
    if not addresses or any(_unsafe_address(address) for address in addresses):
        raise PushRefused(refusal)
    return url


def outcome_for_status(status: int) -> str:
    """`sent`, `gone` (R22: turn the device Off) or `failed` (any other answer, redirects included)."""
    if 200 <= status < 300:
        return "sent"
    return "gone" if status in GONE_STATUSES else "failed"


#: The VAPID token's `sub` claim: one fixed value for every install (the project's public address, the client_uri in
#: docs/client-metadata/corsia-client.json), never the person's email or host. The delivery company reads it on every send.
VAPID_SUB = "https://github.com/cebertowicz/corsia"


def vapid_claims(endpoint: str, expires_at: int) -> dict:
    """The claims the install signs for one send: the delivery company's origin, an expiry, and the fixed `sub`."""
    parts = urlsplit(endpoint)
    return {"aud": f"{parts.scheme}://{parts.hostname}", "exp": expires_at, "sub": VAPID_SUB}


def clear_secrets(row: dict, state: str) -> dict:
    """R13: a row that is not On keeps only its id, endpoint hash, name, date and state; the endpoint and both keys go.
    The R22 update, Turn off, Remove-then-resubscribe and key regeneration all pass through here."""
    if state == "on":
        raise ValueError("an On row keeps its endpoint and keys")
    kept = {key: row.get(key) for key in ("id", "endpoint_hash", "name", "added_at")}
    return {**kept, "state": state, "endpoint": None, "p256dh": None, "auth": None}


async def connect_pinned(host: str, address: str, context: ssl.SSLContext | None = None, port: int = 443):
    """Open the connection to the address the R21 check approved, never resolving again. The certificate is verified against
    the host name (SNI and certificate), never the address: a resolved address whose certificate is not the host's is refused."""
    context = context or ssl.create_default_context()
    if not (context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED):
        raise PushRefused("Certificate checks must stay on.")
    try:
        return await asyncio.wait_for(asyncio.open_connection(address, port, ssl=context, server_hostname=host), SEND_TIMEOUT_SECONDS)
    except (ssl.SSLError, OSError, asyncio.TimeoutError):
        raise PushRefused("Could not verify the delivery company's connection.") from None
