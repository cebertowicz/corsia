"""Which backend serves one email account: the one place a provider name picks Gmail or a mail host.

Every email and invite tool resolves the account, asks :func:`backend_for` for its backend and calls
it; nothing else branches on the provider. A backend supplies only what differs by host: reading,
the draft's reply target and host copy, delivery through the shared claimed-delivery lifecycle
(``tools.action_delivery``), and the calendar list. Microsoft mail has its own tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Protocol

from connections.account_errors import AccountError
from connections.calendar_document import Invite
from connections.connected_accounts import GOOGLE_PROVIDER, MAILHOST_PROVIDER
from tools.gmail_backend import GMAIL
from tools.mailhost_backend import MAIL_HOST

NO_BACKEND = "This account can't be used for email or invitations here."


@dataclass(frozen=True)
class Gate:
    """What one tool says at each step of the reviewed-draft lifecycle, whichever account sends."""

    label: str
    still_sending: str
    claim_error: str


class MailReader(Protocol):
    """Searching and reading one account's mail (``gmail_read`` or ``mailhost.read``)."""

    async def search(self, account_id: str, query: str, max_results: int) -> list[Any]: ...

    async def fetch(self, account_id: str, message_id: str) -> Any: ...

    def check_message_id(self, value: object, account_id: str) -> str: ...


class MailBackend(Protocol):
    @property
    def read(self) -> MailReader: ...

    async def draft_metadata(
        self, account: dict, existing: dict | None, reply_id: object,
    ) -> tuple[dict, str | None, str]:
        """``(target, reply label, signature)`` for a draft about to be saved."""

    async def keep_draft_copy(self, account: dict, text: str, target: dict, previous: dict) -> str | None:
        """Mirror the saved draft on the host; a note when the copy fell short, never an error."""

    async def send_email(self, draft_id: str, account: dict, message: EmailMessage, target: dict, gate: Gate) -> str:
        """Deliver an approved email once; the tool result."""

    async def send_invite(self, draft_id: str, account: dict, invite: Invite, gate: Gate) -> str:
        """Deliver an approved invite once; the tool result."""

    async def list_events(
        self, account_id: str, calendar_id: str, time_min: str, time_max: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """The calendar_list result; ``ValueError`` for a request this host can't answer."""


_BACKENDS: dict[str, MailBackend] = {GOOGLE_PROVIDER: GMAIL, MAILHOST_PROVIDER: MAIL_HOST}


def backend_for(account: dict) -> MailBackend:
    """The backend for a resolved email account; :class:`AccountError` for a provider no backend serves."""
    backend = _BACKENDS.get(str(account.get("provider")))
    if backend is None:
        raise AccountError(NO_BACKEND)
    return backend
