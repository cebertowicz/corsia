"""RFC 6186/6764 SRV discovery for an unknown mail host.

DNS is consulted only for the address's own domain and only before CORSIA asks the
person for server names.  A partial answer is ignored: using it would leave the
person with a row that cannot send mail.  Resolver replies never reach an agent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dns.exception import DNSException

from connections.mailhost.endpoints import Endpoints, Server

Resolve = Callable[[str, str], Any]


def _record(domain: str, service: str, resolver: Resolve) -> Server | None:
    """The highest-priority SRV target for one service, or no usable record."""
    try:
        answers = resolver(f"_{service}._tcp.{domain}", "SRV")
        records = sorted(answers, key=lambda item: (int(item.priority), -int(item.weight)))
        if not records:
            return None
        record = records[0]
        host = str(record.target).rstrip(".").lower()
        port = int(record.port)
    except (AttributeError, TypeError, ValueError, DNSException):
        # Resolver failures and malformed public records mean the person should enter servers instead.
        return None
    if not host or host == "." or not 1 <= port <= 65535:
        return None
    return Server(host, port)


def discover(domain: str, username: str, *, resolver: Resolve | None = None) -> Endpoints | None:
    """Return complete IMAP/SMTP settings found in SRV, otherwise ``None``.

    RFC 6186 calls the TLS IMAP service ``imaps`` and RFC 6409 registers
    ``submission`` for SMTP submission.  RFC 6764's ``caldavs`` is optional.
    """
    if resolver is None:
        import dns.resolver

        resolver = dns.resolver.resolve
    imap = _record(domain, "imaps", resolver)
    smtp = _record(domain, "submission", resolver)
    if imap is None or smtp is None:
        return None
    caldav = _record(domain, "caldavs", resolver)
    caldav_url = f"https://{caldav.host}:{caldav.port}/" if caldav is not None else None
    return Endpoints(name=domain, known=False, username=username, imap=imap, smtp=smtp, caldav=caldav_url)
