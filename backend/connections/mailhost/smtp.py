"""The SMTP login check: TLS first, then AUTH, never the other way round.

On a STARTTLS port the password is sent only after the server offered STARTTLS and it
completed; a server that doesn't offer it is unreachable and receives no AUTH. A port given
with no number is tried as implicit TLS (465), then STARTTLS (587). A host's reply is read
only to name a cause. ``smtplib`` debug output is never enabled.
"""

from __future__ import annotations

import smtplib
import socket
import ssl

from connections.mailhost import tls
from connections.mailhost.endpoints import Endpoints, Server
from connections.mailhost.errors import PASSWORD_REFUSED, SERVER_UNREACHABLE, MailHostError

TIMEOUT_SECONDS = 20.0
_TRANSPORT = (OSError, ssl.SSLError, socket.timeout, smtplib.SMTPException)


def _login_on_port(ep: Endpoints, port: int, username: str, password: str) -> None:
    unreachable = MailHostError(SERVER_UNREACHABLE, host=ep.smtp.host)
    starttls = Server(ep.smtp.host, port).tls_starts_with_starttls(imap=False)
    try:
        if starttls:
            client: smtplib.SMTP = smtplib.SMTP(ep.smtp.host, port, timeout=TIMEOUT_SECONDS)
        else:
            client = smtplib.SMTP_SSL(ep.smtp.host, port, context=tls.context(), timeout=TIMEOUT_SECONDS)
    except _TRANSPORT:
        raise unreachable from None
    try:
        if starttls:
            client.ehlo()
            if not client.has_extn("starttls"):
                raise unreachable
            client.starttls(context=tls.context())
            client.ehlo()
        client.login(username, password)
    except smtplib.SMTPAuthenticationError:
        raise MailHostError(PASSWORD_REFUSED, name=ep.name) from None
    except MailHostError:
        raise
    except _TRANSPORT:
        raise unreachable from None
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing a dead connection has nothing left to report
            pass


def check_login(ep: Endpoints, password: str) -> None:
    """Log in once (a refused password stops at once); an unreachable port falls to the next."""
    unreachable: MailHostError | None = None
    for port in ep.smtp_ports():
        try:
            _login_on_port(ep, port, ep.username, password)
            return
        except MailHostError as exc:
            if exc.cause == PASSWORD_REFUSED:
                raise
            unreachable = exc
    raise unreachable or MailHostError(SERVER_UNREACHABLE, host=ep.smtp.host)
