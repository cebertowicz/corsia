"""SMTP login and submission: TLS before AUTH and never retry after submission starts.

A port with no number is tried as implicit TLS (465), then STARTTLS (587).  Port
fallback ends when an authenticated client is available: a later transport
failure has an unknown outcome and must reach the reviewed-draft lifecycle.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import getaddresses

from connections.account_errors import AccountError
from connections.mail.attachments import OVERSIZE
from connections.mailhost import tls
from connections.mailhost.endpoints import Endpoints, Server
from connections.mailhost.errors import (
    MESSAGE_REFUSED, PASSWORD_REFUSED, RECIPIENTS_REFUSED, SENDER_REFUSED, SERVER_UNREACHABLE, MailHostError,
)

TIMEOUT_SECONDS = 20.0
_LOGIN_TRANSPORT = (OSError, ssl.SSLError, socket.timeout, smtplib.SMTPException)
# Each refusal proves nothing was accepted; its cause decides what the person is told to check.
_SUBMISSION_REFUSED: tuple[tuple[type[smtplib.SMTPException], str], ...] = (
    (smtplib.SMTPRecipientsRefused, RECIPIENTS_REFUSED),
    (smtplib.SMTPSenderRefused, SENDER_REFUSED),
    (smtplib.SMTPDataError, MESSAGE_REFUSED),
)
_REFUSALS = tuple(kind for kind, _cause in _SUBMISSION_REFUSED)
log = logging.getLogger(__name__)


def close(client: smtplib.SMTP) -> None:
    try:
        client.close()
    except (OSError, smtplib.SMTPException):
        log.debug("SMTP connection close failed after the outcome was already decided", exc_info=True)


def _login_on_port(ep: Endpoints, port: int, username: str, password: str) -> smtplib.SMTP:
    """Connect, require TLS, and authenticate one SMTP client without submitting mail."""
    unreachable = MailHostError(SERVER_UNREACHABLE, host=ep.smtp.host)
    starttls = Server(ep.smtp.host, port).tls_starts_with_starttls(imap=False)
    try:
        client: smtplib.SMTP
        if starttls:
            client = smtplib.SMTP(ep.smtp.host, port, timeout=TIMEOUT_SECONDS)
        else:
            client = smtplib.SMTP_SSL(ep.smtp.host, port, context=tls.context(), timeout=TIMEOUT_SECONDS)
    except _LOGIN_TRANSPORT:
        raise unreachable from None
    try:
        if starttls:
            client.ehlo()
            if not client.has_extn("starttls"):
                raise unreachable
            client.starttls(context=tls.context())
            client.ehlo()
        client.login(username, password)
        return client
    except smtplib.SMTPAuthenticationError:
        close(client)
        raise MailHostError(PASSWORD_REFUSED, name=ep.name) from None
    except MailHostError:
        close(client)
        raise
    except _LOGIN_TRANSPORT:
        close(client)
        raise unreachable from None


def logged_in_client(ep: Endpoints, username: str, password: str) -> smtplib.SMTP:
    """Return one authenticated client; only connection/authentication failures fall to another port."""
    unreachable: MailHostError | None = None
    for port in ep.smtp_ports():
        try:
            return _login_on_port(ep, port, username, password)
        except MailHostError as exc:
            if exc.cause == PASSWORD_REFUSED:
                raise
            unreachable = exc
    raise unreachable or MailHostError(SERVER_UNREACHABLE, host=ep.smtp.host)


def check_login(ep: Endpoints, password: str) -> None:
    """Prove SMTP credentials work; a refused password stops at once."""
    client = logged_in_client(ep, ep.username, password)
    close(client)


def _recipients(message: EmailMessage) -> list[str]:
    return [address for _, address in getaddresses(message.get_all("To", []) + message.get_all("Cc", [])) if address]


def send_message(
    ep: Endpoints, username: str, password: str, message: EmailMessage, *, from_addr: str,
) -> list[str]:
    """Submit once after login and return any refused recipient addresses.

    SMTP transport errors during or after submission deliberately propagate: delivery may
    have happened, so callers keep the reviewed draft claimed and never retry it.
    """
    client = logged_in_client(ep, username, password)
    try:
        refused = client.send_message(message, from_addr=from_addr, to_addrs=_recipients(message))
    except _REFUSALS as exc:
        if getattr(exc, "smtp_code", None) == 552 and any(message.iter_attachments()):
            raise AccountError(OVERSIZE) from None
        cause = next(cause for kind, cause in _SUBMISSION_REFUSED if isinstance(exc, kind))
        raise MailHostError(cause, host=ep.name) from None
    finally:
        close(client)
    return list(refused)
