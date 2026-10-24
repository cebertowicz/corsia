"""Why a mail-host call failed, as a named cause with fixed, credential-free text.

A host's reply is outside content: it is mapped to one of these causes where it is read
and its text goes no further, so a row, a log line or a tool result never carries it.
"""

from __future__ import annotations

from connections.account_errors import AccountError

SERVER_UNREACHABLE = "server_unreachable"
PASSWORD_REFUSED = "password_refused"
IMAP_OFF = "imap_off"
CALENDAR_NOT_OFFERED = "calendar_not_offered"
RECIPIENTS_REFUSED = "recipients_refused"
SENDER_REFUSED = "sender_refused"
MESSAGE_REFUSED = "message_refused"
COPY_REFUSED = "copy_refused"
CAUSES = frozenset((
    SERVER_UNREACHABLE, PASSWORD_REFUSED, IMAP_OFF, CALENDAR_NOT_OFFERED, RECIPIENTS_REFUSED, SENDER_REFUSED,
    MESSAGE_REFUSED, COPY_REFUSED,
))

_MESSAGES = {
    SERVER_UNREACHABLE: "CORSIA couldn’t reach {host}. Check the server names with your host or IT.",
    PASSWORD_REFUSED: "{name} didn’t accept this app password. Make a new one in your {name} account settings.",
    IMAP_OFF: "{name} has email access for other apps turned off. Turn on IMAP in {name} settings, then try again.",
    CALENDAR_NOT_OFFERED: "{name} didn’t offer a calendar to CORSIA, so only email is connected.",
    RECIPIENTS_REFUSED: "{host} refused the recipient addresses, so it wasn’t sent. Check the addresses.",
    SENDER_REFUSED: (
        "{host} refused to send from this account, so it wasn’t sent. Check the account in the person’s own mail app."
    ),
    MESSAGE_REFUSED: (
        "{host} refused this message, so it wasn’t sent. It may be too large or look like spam to the host; "
        "ask the person what to change."
    ),
    COPY_REFUSED: "The copy wasn’t saved: {name} refused it, so the folder may be full or read-only.",
}


class MailHostError(AccountError):
    """A named, fixed-text mail-host failure; ``host`` is the server the cause names."""

    def __init__(self, cause: str, *, host: str = "", name: str = "") -> None:
        if cause not in CAUSES:
            raise ValueError("unknown mail-host cause")
        self.cause = cause
        self.host = host
        super().__init__(_MESSAGES[cause].format(host=host or "the server", name=name or "The host"))
