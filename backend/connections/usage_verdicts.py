"""The one rule for which stored vendor verdict applies to a model.

``connections.harness_usage`` is the sole persisted-state owner and calls this
module; nothing here reads a database or performs I/O. Each connection row is
one account (MA-2: one account's limit never marks another limited), so the
verdict map it stores is keyed only by scope within that account: the
normalized model tier, or ``ACCOUNT_SCOPE`` for an account-wide verdict.

A limited verdict carries its own optional release marker (``release``), so the
reset deadline has the verdict's key and lifetime: a new verdict replaces the
entry and with it the marker. ``release_due`` is the only rule for which scopes
need a release driven.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from connections.usage_windows import parse_time

ACCOUNT_SCOPE = "null"
#: Claude plan windows that limit the whole account.
ACCOUNT_WINDOWS = ("five_hour", "seven_day")
_CODEX_PLAN_WINDOWS = ("codex_primary", "codex_secondary")
_TIER_WINDOW = "seven_day_"
#: Release marker states. ``exhausted`` records that the held heads of a window
#: that kept failing were settled, so the window is never driven again.
RELEASE_STATES = ("started", "no_eligible_head", "failed", "exhausted")


@dataclass(frozen=True, slots=True)
class LimitSubject:
    """The key a verdict and its release live under: one account and one scope.

    Each account's verdicts live in its own usage state, so one account's limit
    never marks another limited (MA-2). ``receipt`` is the login the limit was
    observed on; its effects apply only while that login is current.
    """

    provider: str
    scope_key: str
    account_id: str
    receipt: Any = field(default=None, compare=False, repr=False)

    @property
    def scope_model(self) -> str | None:
        """The model this scope limits, or ``None`` for the whole account."""
        return None if self.scope_key == ACCOUNT_SCOPE else self.scope_key


@dataclass(frozen=True, slots=True)
class DueRelease:
    """One limited scope whose reset has passed and whose release must be driven.

    ``exhausted`` means the window's release failed ``max_attempts`` times: its
    held heads are settled as failed instead of probed again.
    """

    subject: LimitSubject
    resets_at: str
    reason: str | None
    attempts: int
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionAvailability:
    """Read-only availability projection for one selected harness model."""

    state: Literal["allowed", "limited_with_reset", "limited_without_reset", "unknown"]
    connection: str | None
    reason: str | None = None
    resets_at: str | None = None
    observed_at: str | None = None
    check_error: str | None = None


@dataclass(frozen=True, slots=True)
class Effective:
    """The verdicts that decide, and describe, one model's availability.

    ``blocking`` is the most recent active limit; ``expired`` is every limited
    candidate whose reset has passed, which settlement handles scope by scope;
    ``latest`` is the most recent verdict of any state, for display only, and
    never decides admission. ``spending`` means a stored plan-exhaustion verdict
    does not block because the account spends past its plan limit on paid capacity.
    """

    blocking: dict[str, Any] | None
    latest: dict[str, Any] | None
    expired: tuple[dict[str, Any], ...] = ()
    spending: bool = False


def epoch_iso(epoch: object) -> str | None:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def scope_key(scope: object) -> str:
    """Normalize a stored or reported scope: a lowercased tier, or the account."""
    if isinstance(scope, str) and scope and scope.lower() != ACCOUNT_SCOPE:
        return scope.lower()
    return ACCOUNT_SCOPE


def scope_matches(model: str | None, key: str) -> bool:
    """The account scope covers every model; a tier covers the models it names."""
    return key == ACCOUNT_SCOPE or (isinstance(model, str) and key in model.lower())


def window_scope(kind: object, *, limit_id: object = None) -> str | None:
    """Map a vendor plan-window kind to its scope; ``None`` is not a plan window.

    A window carrying a ``limit_id`` is a per-limit bucket for another meter,
    never the binding plan window.
    """
    if limit_id:
        return None
    if kind in ACCOUNT_WINDOWS or kind in _CODEX_PLAN_WINDOWS:
        return ACCOUNT_SCOPE
    if isinstance(kind, str) and kind.startswith(_TIER_WINDOW) and len(kind) > len(_TIER_WINDOW):
        return scope_key(kind.removeprefix(_TIER_WINDOW))
    return None


def verdicts(usage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stored verdict map keyed by normalized scope."""
    stored = usage.get("vendor_verdicts")
    if not isinstance(stored, dict):
        return {}
    return {scope_key(scope): verdict for scope, verdict in stored.items() if isinstance(verdict, dict)}


def _observed(verdict: dict[str, Any]) -> datetime:
    return parse_time(verdict.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc)


def effective_verdict(
    usage: dict[str, Any] | None, model: str | None, now: datetime, provider: str | None = None,
) -> Effective:
    """Apply the account verdict and every tier verdict that names ``model``.

    Scopes are independent: an allowed tier verdict never clears an account
    limit, and a model of no named tier sees only the account scope. ``provider``
    names the account's vendor so ``overage_policy.spends_past_plan`` can say
    which plan limits paid capacity covers; without it every active limit blocks.
    """
    if not isinstance(usage, dict):
        return Effective(None, None)
    candidates = [verdict for key, verdict in verdicts(usage).items() if scope_matches(model, key)]
    active = [verdict for verdict in candidates if _active_limit(verdict, now)]
    spendable: list[dict[str, Any]] = []
    if provider is not None and active:
        from connections.overage_policy import spends_past_plan  # overage_policy reads this module

        spendable = [verdict for verdict in active if spends_past_plan(provider, usage, verdict)]
        active = [verdict for verdict in active if verdict not in spendable]
    return Effective(
        blocking=max(active, key=_observed) if active else None,
        latest=max(candidates, key=_observed) if candidates else None,
        expired=tuple(verdict for verdict in candidates if _expired_limit(verdict, now)),
        spending=bool(spendable) and not active,
    )


def _active_limit(verdict: dict[str, Any], now: datetime) -> bool:
    reset = parse_time(verdict.get("resets_at"))
    return verdict.get("state") == "limited" and (reset is None or reset > now)


def _expired_limit(verdict: dict[str, Any], now: datetime) -> bool:
    reset = parse_time(verdict.get("resets_at"))
    return verdict.get("state") == "limited" and reset is not None and reset <= now


def release_marker(verdict: dict[str, Any]) -> dict[str, Any] | None:
    """The verdict's release marker for its own reset window, else ``None``.

    A marker left from an earlier window (a different ``resets_at``) is stale
    and reads as absent.
    """
    release = verdict.get("release")
    if not isinstance(release, dict):
        return None
    reset = parse_time(verdict.get("resets_at"))
    if reset is None or parse_time(release.get("resets_at")) != reset:
        return None
    return release


def release_due(
    usage: dict[str, Any] | None, now: datetime, *, account: LimitSubject,
    max_attempts: int, backoff_seconds: int,
) -> list[DueRelease]:
    """Every limited scope whose stored reset has passed and needs driving now.

    Due means no marker for this window, or a ``failed`` marker whose backoff
    (``backoff_seconds`` doubling per attempt) has elapsed. A failed window at
    ``max_attempts`` is due once more as ``exhausted``. A ``started``,
    ``no_eligible_head`` or ``exhausted`` marker is never due.
    """
    if not isinstance(usage, dict):
        return []
    due = []
    for key, verdict in verdicts(usage).items():
        if not _expired_limit(verdict, now):
            continue
        reset = str(verdict["resets_at"])
        subject = replace(account, scope_key=key)
        release = release_marker(verdict)
        attempts = _attempts(release)
        if release is None:
            due.append(DueRelease(subject, reset, verdict.get("reason"), 0))
        elif release.get("state") != "failed":
            continue
        elif attempts >= max_attempts:
            due.append(DueRelease(subject, reset, verdict.get("reason"), attempts, exhausted=True))
        elif _backoff_elapsed(release, attempts, now, backoff_seconds):
            due.append(DueRelease(subject, reset, verdict.get("reason"), attempts))
    return due


def _attempts(release: dict[str, Any] | None) -> int:
    value = release.get("attempts") if isinstance(release, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _backoff_elapsed(release: dict[str, Any], attempts: int, now: datetime, base: int) -> bool:
    at = parse_time(release.get("at"))
    return at is None or at + timedelta(seconds=base * 2 ** max(0, attempts - 1)) <= now


def next_marker(
    verdict: dict[str, Any], state: str, *, at: str, thread_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The marker a release outcome writes: ``failed`` counts one more attempt."""
    if state not in RELEASE_STATES:
        raise ValueError(f"unknown release state: {state}")
    attempts = _attempts(release_marker(verdict)) + (1 if state == "failed" else 0)
    return {
        "resets_at": verdict.get("resets_at"), "state": state, "attempts": attempts,
        "error": error, "thread_id": thread_id, "at": at,
    }


def availability_from_projection(connection: str, projection: dict[str, Any]) -> ConnectionAvailability:
    """Translate the canonical projection without adding a second verdict rule."""
    check = projection.get("check")
    check_error = check.get("error") if isinstance(check, dict) else None
    if projection["state"] == "limited":
        reset = projection.get("resets_at")
        limited_state: Literal["limited_with_reset", "limited_without_reset"] = (
            "limited_with_reset" if reset else "limited_without_reset"
        )
        return ConnectionAvailability(
            limited_state, connection, projection.get("reason"), reset,
            projection.get("observed_at"), check_error,
        )
    normal_state: Literal["allowed", "unknown"] = "allowed" if projection["state"] == "allowed" else "unknown"
    return ConnectionAvailability(
        normal_state, connection, projection.get("reason"), projection.get("resets_at"),
        projection.get("observed_at"), check_error,
    )
