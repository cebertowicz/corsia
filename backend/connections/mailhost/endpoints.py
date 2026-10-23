"""A mail-host account's non-secret settings: servers, ports, how TLS starts, the username.

Stored as JSON in ``connected_accounts.endpoints_json``; the app password is never here, it
lives in the account's own Keys entry. Every refusal text is fixed and echoes no input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

IMAP_DEFAULT_PORT = 993
#: SMTP with no port given: implicit TLS first, then STARTTLS.
SMTP_AUTO_PORTS = (465, 587)
#: Ports whose TLS starts with STARTTLS; every other port is TLS from the first byte.
IMAP_STARTTLS_PORTS = frozenset((143,))
SMTP_STARTTLS_PORTS = frozenset((587, 25))
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ADDRESS = re.compile(r"^[^\s@<>\",;:\\()\[\]]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

BAD_ADDRESS = "That isn’t an email address like name@example.com."
BAD_SERVER = "{field} must be a server name such as mail.example.com, optionally with a port such as mail.example.com:993."
BAD_CALDAV = "caldav must be an https address such as https://caldav.example.com/."


class EndpointsRefused(ValueError):
    """A request the settings refuse; the text is fixed and safe to show."""


@dataclass(frozen=True)
class Server:
    host: str
    port: int | None = None

    def tls_starts_with_starttls(self, *, imap: bool) -> bool:
        return self.port in (IMAP_STARTTLS_PORTS if imap else SMTP_STARTTLS_PORTS)

    def as_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port}


def parse_address(value: object) -> str:
    if not isinstance(value, str) or not _ADDRESS.match(value.strip()):
        raise EndpointsRefused(BAD_ADDRESS)
    return value.strip()


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[1].lower()


def parse_server(value: object, *, field_name: str) -> Server:
    """``host`` or ``host:port``; a scheme, path or anything else is refused."""
    text = value.strip() if isinstance(value, str) else ""
    host, _, port_text = text.partition(":")
    if not _HOST.match(host) or ".." in host or (":" in text and not port_text.isdigit()):
        raise EndpointsRefused(BAD_SERVER.format(field=field_name))
    port = int(port_text) if port_text else None
    if port is not None and not 1 <= port <= 65535:
        raise EndpointsRefused(BAD_SERVER.format(field=field_name))
    return Server(host.lower(), port)


def parse_caldav(value: object) -> str:
    parts = urlsplit(value.strip()) if isinstance(value, str) else None
    if parts is None or parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise EndpointsRefused(BAD_CALDAV)
    return parts.geturl()


@dataclass(frozen=True)
class Endpoints:
    #: How the row names its host: a known host's name, else the address's domain.
    name: str
    known: bool
    username: str
    imap: Server
    smtp: Server
    caldav: str | None = None
    #: Calendar collections the last check found, each an https address on the CalDAV host.
    calendars: tuple[str, ...] = ()
    app_password_url: str | None = None
    click_path: str | None = None
    #: A host step to do before this works (Zoho and GMX ship with IMAP off).
    enable_imap_step: str | None = None
    #: Other username forms to try when the first is refused (iCloud accepts the bare name).
    username_alternates: tuple[str, ...] = field(default=())
    #: The username forms the host accepted at the last check (None until one has).
    imap_username: str | None = None
    caldav_username: str | None = None
    #: True only when the host has been verified to save SMTP deliveries to Sent itself.
    smtp_saves_sent: bool | None = None

    def imap_port(self) -> int:
        return self.imap.port or IMAP_DEFAULT_PORT

    def smtp_ports(self) -> tuple[int, ...]:
        return (self.smtp.port,) if self.smtp.port else SMTP_AUTO_PORTS

    def password_destinations(self) -> list[str]:
        """Every distinct host the password is sent to, for the Keys card line."""
        hosts = {self.imap.host, self.smtp.host}
        if self.caldav:
            hosts.add(urlsplit(self.caldav).hostname or "")
        return sorted(host for host in hosts if host)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "known": self.known, "username": self.username,
            "imap": self.imap.as_dict(), "smtp": self.smtp.as_dict(), "caldav": self.caldav,
            "calendars": list(self.calendars), "app_password_url": self.app_password_url,
            "click_path": self.click_path, "enable_imap_step": self.enable_imap_step,
            "username_alternates": list(self.username_alternates),
            "imap_username": self.imap_username, "caldav_username": self.caldav_username,
            "smtp_saves_sent": self.smtp_saves_sent,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Endpoints | None:
        """The stored settings, or None for anything that is not a complete, valid set."""
        if not isinstance(raw, dict):
            return None
        try:
            imap, smtp = raw["imap"], raw["smtp"]
            return cls(
                name=str(raw["name"]), known=bool(raw["known"]), username=str(raw["username"]),
                imap=Server(str(imap["host"]), imap.get("port")), smtp=Server(str(smtp["host"]), smtp.get("port")),
                caldav=raw.get("caldav"), calendars=tuple(str(url) for url in raw.get("calendars") or ()),
                app_password_url=raw.get("app_password_url"), click_path=raw.get("click_path"),
                enable_imap_step=raw.get("enable_imap_step"),
                username_alternates=tuple(str(name) for name in raw.get("username_alternates") or ()),
                imap_username=raw.get("imap_username"), caldav_username=raw.get("caldav_username"),
                smtp_saves_sent=raw.get("smtp_saves_sent"),
            )
        except (KeyError, TypeError, AttributeError):
            return None

    def with_found(self, **found: Any) -> Endpoints:
        """The same settings with what a check proved: ``imap_username``, ``caldav_username``, ``calendars``."""
        return replace(self, **found)

    def usernames(self, proven: str | None) -> tuple[str, ...]:
        """The username forms to try, the one that last worked first."""
        return tuple(dict.fromkeys(name for name in (proven, self.username, *self.username_alternates) if name))
