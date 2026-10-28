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


def _default_limit(provider: str) -> dict | None:
    """Read the known active limit for one source's Default account."""
    from connections.harness_usage import active_limit
    from connections.vendor_accounts import resolve_account_id
    from infra.database import get_db

    db = get_db()
    try:
        account_id = resolve_account_id(provider, db=db)
        return active_limit(provider=provider, account_id=account_id, db=db) if account_id else None
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
    """Explain the first signed-in source with a known active plan limit.

    A missing or stale usage observation does not establish a limit, so it
    deliberately returns ``None`` in that case.
    """
    from connections.models import get_connection
    from connections.source_state import READY, row_state
    from infra.db_async import run_in_db

    for provider in ("claude-code", "codex"):
        state = row_state(await run_in_db(get_connection, provider))
        if READY not in (state["state"], state["prior_state"]):
            continue
        limit = await run_in_db(_default_limit, provider)
        if limit is None:
            continue
        source = "Claude Code" if provider == "claude-code" else "Codex"
        reset = limit.get("resets_at")
        local_reset = _local_reset_time(reset) if isinstance(reset, str) else None
        return (f"{source} is signed in but has reached its plan limit"
                f"; it resets {local_reset}." if local_reset else
                f"{source} is signed in but has reached its plan limit.")
    return None


def _verified_default_label(provider: str) -> str | None:
    """Return the verified ambient account label, never a plan or custom name."""
    from connections.account_profiles import account_label
    from connections.vendor_accounts import get_account, resolve_account_id
    from infra.database import get_db

    db = get_db()
    try:
        account_id = resolve_account_id(provider, db=db)
        if account_id is None:
            return None
        label = account_label(get_account(account_id, db=db))
    except Exception:
        log.warning("Could not read the verified %s account label", provider, exc_info=True)
        return None
    finally:
        db.close()
    text = label.get("text")
    email = label.get("email")
    return text if isinstance(text, str) and isinstance(email, str) and email else None


async def subscription_available() -> SubscriptionSource | None:
    """The first subscription source that works now, with a verified label when one exists.

    A source counts only when its last check found it ready (or could not tell
    but it was ready before: still usable by the source-state contract) and no
    plan limit is active. A ``needs_auth`` result never carries a prior ready,
    and a source that was never checked does not count: a CLI that is merely
    installed is not a working provider.
    """
    from connections.models import get_connection
    from connections.source_state import READY, UNVERIFIED, row_state
    from infra.db_async import run_in_db

    for provider in ("claude-code", "codex"):
        state = row_state(await run_in_db(get_connection, provider))
        usable = state["state"] == READY or (
            state["state"] == UNVERIFIED and state["prior_state"] == READY)
        if usable and await run_in_db(_default_limit, provider) is None:
            return SubscriptionSource(provider, await run_in_db(_verified_default_label, provider))
    return None
