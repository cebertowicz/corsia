"""Not moving: open work exists and none of it can move (stuck plans R9).

The one predicate (``read``) is evaluated once per operator projection. Its
facts travel as internal display metadata so chat-list liveness and the agent's
status digest cannot disagree; the human plan header still uses only the four
visible states.

The leaf counts (``operator_node_state.DISTRIBUTION_KEYS``) rule out motion: a
leaf moves when it runs or awaits its owning agent's review. A
``pending_ready`` leaf (nothing holds it, so it is ready to start) moves only
when it is the agent's: a ready task assigned to the user needs the user
(#2181 keeps it from ever being the agent's next step). The counts cannot tell a user's blocker from a derived one (a failed
prerequisite, a delegated child that cannot finish), so the causes each
presented leaf already carries decide that, and name the parts, at no query.
A derived blocker is the agent's to re-plan, so the plan is not still.
A held step whose delegated child is still working counts its own hold, but
something is running, so the plan is not still (R9: "and nothing is running").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

#: Buckets whose leaves always move.
MOTION_KEYS = ("active", "review_parent_agent")
#: Open buckets whose leaves cannot move on their own.
STILL_KEYS = ("review_human", "waiting", "blocked", "paused")
#: How long past its deadline a wait may sit before it reads overdue. Only an
#: armed watch stays a wait, and the expiry sweep retires it on its own: measured
#: on production over 14 days (834 expiries), the sweep fires 45s after the
#: deadline at the median and 637s at the 90th percentile, so 11 minutes clears
#: normal delivery and leaves a wait the sweep has not retired reading overdue.
OVERDUE_GRACE = timedelta(minutes=11)
#: Overdue waits the note names, in plan order; the rest are counted. The agent
#: settles a few a turn and the next note names the next, and three full ids
#: with titles cut to ``close_next_bounds.TITLE_MAX_BYTES`` as emitted keep the
#: names near 450 bytes of the 2 KiB checkpoint projection
#: (``status_context.CHECKPOINT_BYTES``), whatever script a title is in.
NAMED_OVERDUE = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def read_from_facts(
    counts: dict[str, int],
    *,
    user_ready: int,
    user_blocked: int,
    paused_by_user: int,
    wait_deadlines: list[tuple[str, str, dict[str, str] | None]],
    child_running: bool = False,
    set_aside: int | None = None,
) -> dict[str, Any] | None:
    """Reduce aggregate leaf facts into the canonical still-plan summary.

    Each wait deadline travels with the leaf it belongs to, or ``None`` where
    the caller has no name for it. The overdue count and
    ``overdue_wait_entries`` (the named overdue leaves, present only when there
    are any) come from one pass at one clock.
    """
    if not still_count(counts) or any(counts[key] for key in MOTION_KEYS):
        return None
    if child_running or user_blocked < counts["blocked"]:
        return None
    if user_ready < counts["pending_ready"]:
        return None
    deadlines = [
        (earliest, latest, leaf)
        for first, last, leaf in wait_deadlines
        if (earliest := parse_deadline(first)) is not None
        if (latest := parse_deadline(last)) is not None
    ]
    cutoff = _utcnow() - OVERDUE_GRACE
    overdue_leaves = [leaf for _earliest, latest, leaf in deadlines if latest[0] < cutoff]
    overdue = len(overdue_leaves)
    named = [leaf for leaf in overdue_leaves if leaf is not None]
    turns_overdue = [
        latest[0] + OVERDUE_GRACE
        for _earliest, latest, _leaf in deadlines
        if latest[0] >= cutoff
    ]
    earliest = min(
        (first for first, _last, _leaf in deadlines), default=None,
    )
    facts: dict[str, Any] = {
        "need_you": counts["review_human"] + counts["blocked"] + user_ready,
        "next_wait_ends_at": earliest[1] if earliest is not None else None,
        "next_overdue_at": min(turns_overdue).isoformat() if turns_overdue else None,
        "overdue_waits": overdue,
        "paused_by_you": paused_by_user,
    }
    if named:
        facts["overdue_wait_entries"] = named
    return {**facts, "set_aside": set_aside} if set_aside is not None else facts


def read(
    plan: dict, counts: dict[str, int], nodes: list[dict], users: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Return the parts a still plan waits on, or ``None`` if anything can move.

    ``need_you`` is human reviews and decisions (both read ``review_human``)
    plus leaves blocked by a hold the user owns and ready leaves assigned to
    the user (``users``, their ids); a raised decision's source task is not
    counted again. ``next_wait_ends_at`` is the earliest end of a waiting
    leaf's deadlines. ``overdue_waits`` counts waiting leaves whose every
    deadline passed more than ``OVERDUE_GRACE`` ago, and
    ``overdue_wait_entries`` names those leaves in plan order. ``paused_by_you``
    counts leaves the user paused. The plan is still only when every ready leaf
    the counts hold is shown to be the user's.
    """
    from plans import node_states, operator_node_state, operator_scheduled

    user_ready = user_blocked = paused_by_user = set_aside = 0
    deadlines: list[tuple[str, str, dict[str, str] | None]] = []
    has_child_running = False
    for node in node_states.leaves(nodes):
        bucket = operator_node_state.leaf_bucket(node)
        causes = node["execution"]["causes"]
        has_child_running = has_child_running or child_running(causes)
        if bucket == "pending_ready" and node["id"] in users:
            user_ready += 1
        if bucket == "blocked" and user_blocker(causes):
            user_blocked += 1
        if bucket == "paused" and (plan["state"] == "paused" or _user_pause(causes)):
            paused_by_user += 1
        if bucket == "waiting":
            set_aside += operator_scheduled.only_set_aside(causes)
            found = deadline_range(causes)
            if found is not None:
                leaf = {"title": str(node["title"]), "node_id": str(node["id"])}
                deadlines.append((*found, leaf))
    return read_from_facts(
        counts,
        user_ready=user_ready,
        user_blocked=user_blocked,
        paused_by_user=paused_by_user,
        wait_deadlines=deadlines,
        child_running=has_child_running,
        set_aside=set_aside,
    )


def still_count(counts: dict[str, int]) -> int:
    """Open leaves that do not run; in a still plan every ready one is the user's."""
    return counts["pending_ready"] + sum(counts[key] for key in STILL_KEYS)


def note(facts: dict[str, Any]) -> str:
    """The digest's line: the header's three parts, then what the agent owes."""
    need = facts["need_you"]
    said = [f"{need} need{'s' if need == 1 else ''} the user"] if need else []
    ends = facts["next_wait_ends_at"]
    if ends:
        said.append(f"next wait ends {ends}")
    if facts["paused_by_you"]:
        said.append(f"{facts['paused_by_you']} paused by the user")
    overdue = facts["overdue_waits"]
    head = "not moving: " + " · ".join(said) if said else "not moving"
    if overdue:
        plural = "waits are" if overdue > 1 else "wait is"
        from plans.close_next_bounds import TITLE_MAX_BYTES, bounded

        entries = facts.get("overdue_wait_entries", [])
        named = ", ".join(
            f"{bounded(entry['title'], TITLE_MAX_BYTES)} ({entry['node_id']})"
            for entry in entries[:NAMED_OVERDUE]
        )
        if len(entries) > NAMED_OVERDUE:
            named += f" and {len(entries) - NAMED_OVERDUE} more"
        return (
            f"{head} · {overdue} {plural} past the deadline{': ' + named if named else ''}. "
            "Check each: resolve it if it cleared, wait again with a new deadline and a reason, "
            "raise a decision, or close the task."
        )
    set_aside = facts.get("set_aside", 0)
    if set_aside:
        return (
            f"{head}. You scheduled {set_aside} of these yourself; nothing releases them early "
            "unless you do. Review them in plan order and start any you can drive now."
        )
    owed = f"until {ends}" if ends else "until something changes"
    return f"{head}. Pick up anything you can move; otherwise nothing is owed {owed}."


def user_blocker(causes: list[dict]) -> bool:
    return any(
        cause.get("placed") and cause["kind"] == "blocked" and cause["owner_kind"] == "user"
        for cause in causes
    )


def child_running(causes: list[dict]) -> bool:
    from plans import wait_causes

    return any(
        cause.get("reason_key") == wait_causes.DELEGATED_CHILD and cause.get("child_active", True)
        for cause in causes
    )


def _user_pause(causes: list[dict]) -> bool:
    from plans import user_steering

    pause = user_steering.placed_pause(causes)
    return pause is not None and user_steering.is_user_pause(pause)


def parse_deadline(value: str) -> tuple[datetime, str] | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc), value


def late_wake(deadline: str | None, now: str) -> str | None:
    """A late watch's wake text (EW2/EW5), or None when it fires on time.

    A watch fires within ``OVERDUE_GRACE`` of its deadline in normal delivery;
    one that fires later missed its time, so the agent is told to settle it.
    """
    due, fired = parse_deadline(str(deadline or "")), parse_deadline(now)
    if due is None or fired is None or fired[0] - due[0] <= OVERDUE_GRACE:
        return None
    return (
        f"This wait was due at {due[1]} and its wake came late. Resolve it if "
        "what you waited for arrived; otherwise arm_watch it again with a new "
        "deadline and reason."
    )


def deadline_range(causes: list[dict]) -> tuple[str, str] | None:
    """Earliest/latest raw deadline strings ordered by the canonical instant."""
    found = _deadlines(causes)
    return (min(found)[1], max(found)[1]) if found else None


def _deadlines(causes: list[dict]) -> list[tuple[datetime, str]]:
    return [
        parsed
        for cause in causes
        if cause.get("kind") == "waiting" and cause.get("ends_at")
        if (parsed := parse_deadline(str(cause["ends_at"]))) is not None
    ]
