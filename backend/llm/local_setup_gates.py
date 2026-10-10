"""The checks that decide whether local setup runs, and for which profile.

One rule sizes every profile's context window from host RAM, and the same
window decides eligibility; the subscription check keeps install from
downloading a model nobody needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from llm.local_profiles import DEFAULT_PROFILE, PROFILES, LocalProfile, profile_by_id

log = logging.getLogger(__name__)

# RAM is compared in GiB because Apple sells memory in binary units: a Mac sold
# as "48 GB" reports 48 GiB. Disk sizes are decimal GB, as Finder shows them.
GIB = 1024**3
# Kept free beyond oMLX's own hold-back for macOS, CORSIA and apps. Not a
# vendor-sourced figure: 12.5% equals a 16 GiB reserve at Flash Next's 128 GiB
# threshold, and as a share of RAM it keeps the lowest band eligible from 24 GiB.
RESERVE_SHARE = 0.125
MAX_CONTEXT_WINDOW = 262_144


@dataclass(frozen=True)
class Eligibility:
    context_window: int
    eligible: bool


@dataclass(frozen=True)
class SubscriptionSource:
    provider: str
    label: str | None


def omlx_hold_back_bytes(ram_bytes: int) -> float:
    """What oMLX's memory guard keeps free on its default "balanced" tier.

    ``min(8, max(3, 8% of RAM))`` GiB: oMLX c0b1056 ``omlx/process_memory_enforcer.py``
    ``_TIER_RESERVE`` (60-62) and ``tier_reserve_bytes`` (79-84); default tier
    ``omlx/settings.py:565``.
    """
    return min(8, max(3, 0.08 * ram_bytes / GIB)) * GIB


def context_window(profile: LocalProfile, ram_bytes: int) -> int:
    """The profile's window: RAM less oMLX's hold-back, the reserve and the weights, as KV.

    8K aligned and capped at 256K; 0 when nothing is left.
    """
    spare = (ram_bytes - omlx_hold_back_bytes(ram_bytes) - RESERVE_SHARE * ram_bytes
             - profile.resident_weight_bytes)
    if spare <= 0:
        return 0
    return min(MAX_CONTEXT_WINDOW, int(spare / (profile.kv_mb_per_token * 1e6) // 8192) * 8192)


def host_ram_bytes() -> int:
    """This Mac's RAM, read through its canonical owner."""
    from infra.host_resources import host_total_ram_bytes

    return int(host_total_ram_bytes())


def profile_eligibility(profile: LocalProfile, ram_bytes: int | None = None) -> Eligibility:
    """Whether this Mac meets the profile's RAM band and fits its minimum window."""
    if ram_bytes is None:
        ram_bytes = host_ram_bytes()
    window = context_window(profile, ram_bytes)
    return Eligibility(window, ram_bytes >= profile.min_ram_gib * GIB and window >= profile.min_context)


def profile_for_ram(ram_bytes: int) -> LocalProfile:
    """The profile for the highest RAM band this Mac meets."""
    return [p for p in PROFILES if ram_bytes >= p.min_ram_gib * GIB][-1]


def recommended_profile(ram_bytes: int, foreign_omlx: bool) -> dict | None:
    """The model this Mac's memory picks, as the setup route shows it.

    The default's band counts: the offer runs when install skipped local setup.
    None when the Mac can't run its band's model, or when the oMLX present is
    the person's own, which setup adds no models to.
    """
    profile = profile_for_ram(ram_bytes)
    if foreign_omlx or not profile_eligibility(profile, ram_bytes).eligible:
        return None
    return {
        "id": profile.id, "display_name": profile.display_name,
        "download_size_bytes": profile.download_size_bytes,
        "download_size_gb": round(profile.download_size_bytes / 1000**3, 1),
        "min_ram_gib": profile.min_ram_gib,
    }


class ProfileRefused(ValueError):
    """A requested profile can't be set up here; the message is for the person."""


def resolve_profile(profile_id: str | None, ram_bytes: int, foreign_omlx: bool) -> LocalProfile:
    """The profile to set up: the default when none is named, else the named one if allowed.

    Refused when unknown, when this Mac's RAM is below its band, or when the oMLX
    present is the person's own: setup uses that oMLX as it is and adds no models.
    """
    if profile_id is None:
        return DEFAULT_PROFILE
    profile = profile_by_id(profile_id)
    if profile is None:
        raise ProfileRefused(f"Unknown local model profile {profile_id!r}.")
    if ram_bytes < profile.min_ram_gib * GIB:
        raise ProfileRefused(
            f"{profile.display_name} needs a Mac with at least {profile.min_ram_gib} GB of memory.")
    if foreign_omlx and profile is not DEFAULT_PROFILE:
        raise ProfileRefused(
            "Setup does not add models to an oMLX CORSIA did not install. "
            f"Add {profile.display_name} in your own oMLX instead.")
    return profile


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
