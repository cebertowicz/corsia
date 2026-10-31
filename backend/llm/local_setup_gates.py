"""The checks that decide whether local setup runs at all.

This Mac's memory decides whether there is a local model (``local_model`` owns
that rule and which model it is); the subscription check keeps install from
downloading a model nobody needs.
"""

from __future__ import annotations

import logging
import platform
from functools import cache
from dataclasses import dataclass
from datetime import datetime

from llm.local_model import memory_allows_local

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubscriptionSource:
    provider: str
    label: str | None


@cache
def host_ram_bytes() -> int:
    """This Mac's RAM, read through its canonical owner."""
    from infra.host_resources import host_total_ram_bytes

    return int(host_total_ram_bytes())


def host_never_runs_local() -> bool:
    """Whether the server's operating system is not macOS."""
    return platform.system() != "Darwin"


def local_memory_ok(ram_bytes: int | None = None) -> bool:
    """Whether this Mac has the memory for a local model; reads its RAM when not given."""
    return memory_allows_local(host_ram_bytes() if ram_bytes is None else ram_bytes)


def _signed_in_accounts(provider: str) -> list[tuple[dict, dict | None]]:
    """Each signed-in CORSIA account of a vendor whose source is usable, with its active plan limit.

    A vendor's source counts only through its accounts: one that last checked ready (or could
    not tell but was ready before), and that has an account signed in now. A connection row
    that still reads ready after its account signed out counts for nothing.
    """
    from connections.harness_usage import active_limit
    from connections.models import get_connection
    from connections.source_state import READY, UNVERIFIED, row_state
    from connections.vendor_accounts import list_accounts
    from infra.database import get_db

    db = get_db()
    try:
        state = row_state(get_connection(provider, db=db))
        if not (state["state"] == READY or (state["state"] == UNVERIFIED and state["prior_state"] == READY)):
            return []
        return [(account, active_limit(provider=provider, account_id=account["id"], db=db))
                for account in list_accounts(provider=provider, db=db) if account["state"] == "signed_in"]
    finally:
        db.close()


def _local_reset_time(reset: str) -> str:
    """Render a provider reset in the machine's local timezone when parseable."""
    try:
        moment = datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return reset
    return moment.astimezone().strftime("%a %b %-d, %-I:%M %p %Z")


async def limited_subscription_message() -> str | None:
    """Explain the first source with a signed-in account at a known active plan limit.

    A missing or stale usage observation does not establish a limit, so it
    deliberately returns ``None`` in that case.
    """
    from infra.db_async import run_in_db

    for provider in ("claude-code", "codex"):
        limit = next((limit for _, limit in await run_in_db(_signed_in_accounts, provider)
                      if limit is not None), None)
        if limit is None:
            continue
        source = "Claude Code" if provider == "claude-code" else "Codex"
        reset = limit.get("resets_at")
        local_reset = _local_reset_time(reset) if isinstance(reset, str) else None
        return (f"{source} is signed in but has reached its plan limit"
                f"; it resets {local_reset}." if local_reset else
                f"{source} is signed in but has reached its plan limit.")
    return None


def _verified_label(account: dict) -> str | None:
    """Return the account's label when it names a verified email, never a plan or custom name."""
    from connections.account_profiles import account_label

    try:
        label = account_label(account)
    except Exception:
        log.warning("Could not read the verified %s account label", account["provider"], exc_info=True)
        return None
    text = label.get("text")
    email = label.get("email")
    return text if isinstance(text, str) and isinstance(email, str) and email else None


async def subscription_available() -> SubscriptionSource | None:
    """The first subscription source that works now, with a verified label when one exists.

    A source counts only through a CORSIA account of that vendor that is signed in now
    with no active plan limit, on a source whose last check found it ready (or could not
    tell but it was ready before). A CLI that is merely installed or signed in from a
    terminal is not a working provider.
    """
    from infra.db_async import run_in_db

    for provider in ("claude-code", "codex"):
        for account, limit in await run_in_db(_signed_in_accounts, provider):
            if limit is None:
                return SubscriptionSource(provider, await run_in_db(_verified_label, account))
    return None
