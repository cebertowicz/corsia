"""What an agent and the person are told when the host-access reviewer cannot run.

The runtime outcome (``ReviewerUnavailableError``) carries the cause; this
module owns every sentence built from it, so one helper (``outage_cause``)
turns a cause into plain words for the agent's refusal and Settings alike.
None of it includes reviewer free text: names come from the model catalog and
causes from the runtime's fixed reason codes.
"""

from __future__ import annotations

from datetime import datetime

from host_access.errors import ReviewerOutage, ReviewerUnavailableError
from llm.locality import LOCALITY_REFUSALS
from prompts.cloud_off import LOCALITY_PHRASES

SETTINGS_PLACE = "Settings > System > Host Access Review"

# One notice for many refusals needs a system-notice path that dedupes and
# clears itself; none exists without new machinery, so the agent is asked to
# tell the person once instead.
_TELL_THE_PERSON = (
    "Tell the person once that host-access review is down; they can choose "
    f"another model or turn on fallback in {SETTINGS_PLACE}."
)

# Settings status tails while the chosen reviewer can't run. With fallback on:
FALLBACK_REVIEWING = "Each requesting agent's own model is reviewing instead."
FALLBACK_PARTIAL = " Those requests use the requesting agent's own model until then."
# With fallback off; "until then" is said only when a reset time is known.
OFF_FAIL_UNTIL_THEN = "Host-access requests will fail until then."
OFF_FAIL = "Host-access requests will fail."
OFF_PARTIAL_FAIL_UNTIL_THEN = "Those requests fail until then."
OFF_PARTIAL_FAIL = "Those requests fail."
FALLBACK_OFF_FIX = "Choose another model or turn on fallback."

_PINNED_APPLY_AGAIN = (
    "This lease's reviewer can't run. Apply again; with fallback on, your own "
    "model will review the new application."
)


def _local(value: str | None) -> datetime | None:
    """Parse a reset time into the server's local zone, if it parses."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return None


def _moment_text(moment: datetime) -> str:
    """Month, day and 12-hour time with the zone named, as ``Oct 14, 1:08 AM PDT``."""
    return f"{moment:%b} {moment.day}, {moment.hour % 12 or 12}:{moment:%M %p %Z}"


def outage_cause(outage: ReviewerOutage) -> str:
    """Plain words for why one reviewer cannot run, with its reset time when known."""
    if outage.reason in LOCALITY_REFUSALS:
        return f"it's a cloud model and {LOCALITY_PHRASES[outage.reason]}"
    if outage.reason == "usage_limited":
        moment = _local(outage.resets_at)
        return f"usage limit until {_moment_text(moment)}" if moment else "usage limit"
    return outage.reason.replace("_", " ")


def _resume_moment(exc: ReviewerUnavailableError | None) -> str | None:
    """When review can resume: the earliest reset, known only if every cause has one."""
    if exc is None:
        return None
    outages = [exc.outage, *([exc.fallback] if exc.fallback else [])]
    resets = [_local(outage.resets_at) for outage in outages]
    known = [moment for moment in resets if moment is not None]
    return _moment_text(min(known)) if len(known) == len(resets) else None


def _unavailable_head(exc: ReviewerUnavailableError | None) -> str:
    """The first sentence: which reviewer(s) cannot run."""
    if exc is None:
        return "The host-access reviewer chosen in Settings can't run right now."
    if exc.fallback is None:
        return (
            "The host-access reviewer chosen in Settings can't run right now "
            f"({outage_cause(exc.outage)})."
        )
    return (
        "Neither the host-access reviewer chosen in Settings "
        f"({exc.outage.model_name}: {outage_cause(exc.outage)}) nor the "
        f"requesting agent's own model ({exc.fallback.model_name}: "
        f"{outage_cause(exc.fallback)}) can run right now."
    )


def _locality_fix(exc: ReviewerUnavailableError) -> str | None:
    """The one fix a locality refusal has: the person's setting, and no waiting for it.

    Only the person can lift a locality refusal, so every cause the locality
    owner names gets this fix, never the wait-for-the-reviewer guidance.
    """
    if exc.outage.reason not in LOCALITY_REFUSALS:
        return None
    fix = f"Tell the person once to choose a local reviewer model in {SETTINGS_PLACE}"
    return f"{fix} or turn on reviewer fallback." if not exc.fallback_enabled else f"{fix}."


def application_guidance(
    exc: ReviewerUnavailableError | None, *, subject: str = "application",
) -> str:
    """Tell an agent its review did not happen, why, and what to do instead."""
    if exc is not None and (fix := _locality_fix(exc)):
        return f"{_unavailable_head(exc)} Your {subject} was not reviewed. {fix}"
    moment = _resume_moment(exc)
    wait = (
        f"until {moment}, or until the reviewer is back"
        if moment else "until the reviewer is back"
    )
    return (
        f"{_unavailable_head(exc)} Your {subject} was not reviewed. "
        f"{_TELL_THE_PERSON} Set the task aside {wait}, and continue other work."
    )


def action_guidance(exc: ReviewerUnavailableError) -> str:
    """Guidance for an action whose lease reviewer cannot run (pinned review).

    A pinned review never re-resolves, so neither waiting nor a Settings change
    lifts a locality refusal of its model: only a new application can reach a
    reviewer the chat may use, and that application's own refusal names any fix.
    """
    if exc.pinned and exc.fallback_enabled:
        return _PINNED_APPLY_AGAIN
    if exc.pinned and exc.outage.reason in LOCALITY_REFUSALS:
        return (
            f"This lease's reviewer can't run ({outage_cause(exc.outage)}). Your "
            "action was not reviewed. Apply again; if the new application is "
            "refused too, its refusal names the fix."
        )
    return application_guidance(exc, subject="action")


def action_review_refusal(exc: BaseException) -> str:
    """The refusal for an action review that produced no verdict."""
    cause = exc.__cause__
    if isinstance(cause, ReviewerUnavailableError):
        return action_guidance(cause)
    return (
        f"Host action denied: {exc}. Your lease is still active; "
        "retrying the action triggers a fresh review."
    )
