"""Agent-written event rules: match one event, guard each recipient, record the outcome.

Today's routing decides first and stays in code (the webhook router's legs,
``resolve_targets``, the binder, the self-authored and cancelled-check drops).
Rules read that decision (its ``Target`` list, the owner and lifecycle it
resolved) and never re-derive it. A rule an agent wrote only adds to it:

- a ``deliver`` rule adds a recipient, or rides a delivery routing already made
  to that thread (its note joins it, and a wake beats a digest);
- a ``drop`` rule removes its writer's own delivery, nothing else.

Every outcome of a rule goes through :func:`record`, the one writer of
``event_route_decisions``. With no enabled rule for an event's type in the
event's project, evaluation costs one indexed read and nothing else. Whether a
rule's wake ask wakes is the disposition table's answer. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from events import rule_reads
from events.disposition import WAKE as TABLE_WAKE, disposition
from events.resolve import Target, lifecycle
from registry.retirement_predicate import identity_ended

DELIVER = "deliver"
DROP = "drop"
WAKE = "wake"
DIGEST = "digest"
REGISTERED_OWNER = "registered_owner"
THREAD_PREFIX = "thread:"
PROJECT_PREFIX = "project:"
OPERATORS = ("eq", "not_eq", "in", "prefix", "present")
NAMING = ("eq", "in")  # the operators whose values name what an event must be
MAX_TESTS = 4
NOTE_LIMIT = 500

# Outcomes, as event_route_decisions stores them.
WOKE = "woke"
DIGESTED = "digested"
DROPPED = "dropped"
HELD = "held"
DEAD_LETTERED = "dead_lettered"
ENDED = "ended"
DUPLICATE = "duplicate"

# Reasons a guard gives.
WAKE_CAP = "wake cap"
PAUSED = "paused"
NEVER_WAKES = "this recipient never wakes for this artifact"
OTHER_OWNER = "the registered owner is another agent"
OUT_OF_PROJECT = "recipient outside the rule's project"

WAKES_PER_HOUR = 1
RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class Rule:
    id: str
    scope: str
    connector: str
    event_type: str
    match: tuple[Mapping[str, Any], ...]
    action: str
    to: str
    mode: str | None
    note: str | None
    created_by: str
    enabled: bool = True


@dataclass(frozen=True)
class RuleEvent:
    """One event as rules see it: its declared type and facts, never its raw body."""

    event_id: str
    connector: str
    event_type: str
    watch_event: str  # the name a watch on this event uses
    facts: Mapping[str, Any]
    artifact_ref: Mapping[str, str] | None
    ends_artifact: bool = False


@dataclass(frozen=True)
class Entry:
    """One rule's part in a delivery: whether it asks to wake, and why not."""

    rule_id: str
    wake: bool
    reason: str | None = None  # why its wake ask became a digest (R7, R9, 4.4, the table)
    since_wake: int = 0  # R9: this rule's capped digests to the thread since it last woke it


@dataclass
class Delivery:
    """What rules add to one thread's delivery.

    ``routed_wake`` is today's routing's own choice for a delivery rules ride; a
    delivery rules add has none. Wake beats digest.
    """

    routed_wake: bool = False
    notes: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)

    @property
    def wake(self) -> bool:
        return self.routed_wake or any(entry.wake for entry in self.entries)

    @property
    def capped(self) -> list[Entry]:
        """The rules whose wake the hourly cap turned into this digest."""
        return [] if self.wake else [entry for entry in self.entries if entry.reason == WAKE_CAP]

    def add(self, rule: Rule, wake: bool, reason: str | None, since_wake: int = 0) -> None:
        self.entries.append(Entry(rule.id, wake, reason, since_wake))
        if rule.note:
            self.notes.append(rule.note)

    def outcomes(self, thread: str, delivered: str, *, ride: bool) -> list[Outcome]:
        """Each rule's outcome once the delivery is made (woke, digested or duplicate).

        ``via='rule'`` marks the rule that caused the wake or digest; a rule that
        rode one something else caused is ``via='routed'``. R9 counts only the first.
        """
        results = []
        for entry in self.entries:
            if delivered == WOKE:
                caused = entry.wake and not self.routed_wake
            else:
                caused = not ride
            results.append(Outcome(entry.rule_id, thread, delivered,
                                   via="rule" if caused else "routed", reason=entry.reason))
        return results


@dataclass(frozen=True)
class Outcome:
    rule_id: str
    thread: str | None
    outcome: str
    via: str = "rule"  # "routed": the rule rode a delivery something else made
    reason: str | None = None


@dataclass
class RulePlan:
    """Everything rules decided for one event, before delivery."""

    event: RuleEvent
    lifecycle: str = "active"  # the artifact's, as routing resolved it
    drops: dict[str, str] = field(default_factory=dict)       # routed thread -> drop rule id
    rides: dict[str, Delivery] = field(default_factory=dict)  # routed thread -> what rides it
    adds: dict[str, Delivery] = field(default_factory=dict)   # new thread -> its delivery
    settled: list[Outcome] = field(default_factory=list)      # outcomes with no delivery


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def from_row(row: Mapping[str, Any]) -> Rule:
    return Rule(
        id=row["id"], scope=row["scope"], connector=row["connector"],
        event_type=row["event_type"], match=tuple(json.loads(row["match"])),
        action=row["action"], to=row["to"], mode=row["mode"], note=row["note"],
        created_by=row["created_by"], enabled=bool(row["enabled"]),
    )


def lookup(db: sqlite3.Connection, connector: str, event_type: str, thread: str) -> list[Rule]:
    """The enabled rules for one declared type in ``thread``'s project: the one
    indexed read every event pays. Rules resolve in the artifact owner's scope."""
    rows = db.execute(
        "SELECT * FROM event_rules WHERE scope=? || (SELECT project_id FROM threads WHERE id=?) "
        "AND connector=? AND event_type=? AND enabled=1 ORDER BY created_at, id",
        (PROJECT_PREFIX, thread, connector, event_type),
    ).fetchall()
    return [from_row(row) for row in rows]


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, str) and not isinstance(actual, str) and actual is not None:
        return str(actual) == expected
    return actual == expected


def test_matches(test: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    actual = facts.get(test["field"])
    op = test["op"]
    if op == "present":
        return actual is not None
    value = test.get("value")
    if op == "eq":
        return actual is not None and _same(actual, value)
    if op == "not_eq":
        return not _same(actual, value)
    if op == "in":
        return actual is not None and any(_same(actual, item) for item in value or ())
    if op == "prefix":
        return isinstance(actual, str) and isinstance(value, str) and actual.startswith(value)
    return False


def matches(rule: Rule, facts: Mapping[str, Any]) -> bool:
    return all(test_matches(test, facts) for test in rule.match)


def thread_target(to: str) -> str | None:
    return to.removeprefix(THREAD_PREFIX) if to.startswith(THREAD_PREFIX) else None


def project_scope(project_id: str) -> str:
    return PROJECT_PREFIX + project_id


def routed(
    targets: Iterable[Target], skipped: Iterable[str] = (),
) -> tuple[dict[str, Target], dict[str, Any], str | None]:
    """Routing's decision as rules read it: each thread it delivers to (less the
    caller's ``skipped``), its routing state, and the thread whose project scopes
    the rules (the registered owner, else the first routed thread)."""
    targets, skipped = list(targets), set(skipped)
    state = next((t.routing_state for t in targets if t.routing_state), None) or {}
    by_thread = {t.thread: t for t in targets if t.thread is not None
                 and t.disposition != "dead_letter" and t.thread not in skipped}
    owner = (state.get("owner") or {}).get("parent_thread_id")
    return by_thread, state, owner or next(iter(by_thread), None)


def _paused(db: sqlite3.Connection, thread: str) -> bool:
    from plans.public import thread_paused_since

    return thread_paused_since(db, thread) is not None


def _apply_drops(
    plan: RulePlan, rules: list[Rule], by_thread: Mapping[str, Target],
    recipients: Mapping[str, str | None],
) -> dict[str, str]:
    """Each drop rule removes its writer's own delivery, routed or rule-added, and
    records an outcome only when it removed one; returns writer -> rule."""
    added = {recipients[rule.id] for rule in rules if rule.action == DELIVER}
    dropped_by: dict[str, str] = {}
    for rule in (rule for rule in rules if rule.action == DROP):
        writer = rule.created_by
        dropped_by.setdefault(writer, rule.id)
        if writer in by_thread:
            plan.drops.setdefault(writer, rule.id)
        if writer in by_thread or writer in added:
            plan.settled.append(Outcome(rule.id, writer, DROPPED))
    return dropped_by


def _settle_unreachable(
    plan: RulePlan, rules: list[Rule], recipients: Mapping[str, str | None],
    threads: Mapping[str, Mapping[str, Any]],
) -> list[Rule]:
    """Settle each rule whose affected thread (a delivery's recipient, a drop's
    writer) is unavailable or no longer in the rule's project; return the rest."""
    current = []
    for rule in rules:
        thread = recipients[rule.id] if rule.action == DELIVER else rule.created_by
        row = threads.get(thread) if thread else None
        if rule.action == DELIVER and (row is None or not row["available"]):
            reason = "no registered owner" if thread is None else "recipient unavailable"
            plan.settled.append(Outcome(rule.id, thread, DEAD_LETTERED, reason=reason))
        elif row is not None and row["project_id"] != rule.scope.removeprefix(PROJECT_PREFIX):
            plan.settled.append(Outcome(rule.id, thread, HELD, reason=OUT_OF_PROJECT))
        else:
            current.append(rule)
    return current


def _wake_for(
    db: sqlite3.Connection, rule: Rule, thread: str, row: Mapping[str, Any],
    routed_here: Target | None, artifact_lifecycle: str,
) -> tuple[bool, str | None, int]:
    """Whether this rule wakes the thread, why not, and its capped count.

    The disposition table answers whether this recipient can wake at all; the
    rule's own guards are 4.4 (an agent's ``registered_owner`` rule wakes only
    itself or its temporary workers), R7 (pause) and R9 (hourly budget).
    """
    if rule.mode != WAKE:
        return False, None, 0
    if routed_here is not None and routed_here.disposition == "owner_wake":
        return False, None, 0  # routing already wakes it; the rule only rides
    relationship = routed_here.relationship if routed_here is not None else "rule"
    if routed_here is not None and routed_here.routing_state:
        artifact_lifecycle = lifecycle(routed_here.routing_state)
    if disposition(relationship, "informational", artifact_lifecycle,
                   rule_wake=True) != TABLE_WAKE:
        return False, NEVER_WAKES, 0
    if (rule.to == REGISTERED_OWNER and thread != rule.created_by
            and row["spawned_by"] != rule.created_by):
        return False, OTHER_OWNER, 0  # 4.4
    if _paused(db, thread):
        return False, PAUSED, 0  # R7
    spent, capped = rule_reads.wake_budget(
        db, rule.id, thread, cap=WAKE_CAP, since=_iso(_now() - timedelta(hours=1)))
    if spent >= WAKES_PER_HOUR:
        return False, WAKE_CAP, capped + 1  # R9
    return True, None, 0


def evaluate(
    db: sqlite3.Connection, event: RuleEvent, targets: Iterable[Target],
    skipped: Iterable[str] = (),
) -> RulePlan | None:
    """Decide what agent-written rules add to routing's ``targets`` for one event.

    ``skipped`` are routed threads the caller's own filter keeps this event from.
    Returns ``None`` when no enabled rule for the event's type in its project
    matches its facts; the caller then delivers exactly as it would without rules.
    """
    by_thread, state, anchor = routed(targets, skipped)
    if anchor is None:
        return None  # no owner and nothing routed: no project, so no rule
    rules = [rule for rule in lookup(db, event.connector, event.event_type, anchor)
             if matches(rule, event.facts)]
    if not rules:
        return None
    owner = (state.get("owner") or {}).get("parent_thread_id")
    plan = RulePlan(event, lifecycle=lifecycle(state))
    recipients = {
        rule.id: (owner if rule.to == REGISTERED_OWNER else thread_target(rule.to))
        for rule in rules
    }
    if identity_ended(state.get("identity")) and not event.ends_artifact:
        # R27: an ended artifact's later activity fires no rule.
        plan.settled = [Outcome(rule.id, recipients[rule.id], ENDED) for rule in rules]
        return plan
    threads = rule_reads.threads(
        db, [*recipients.values(), *(rule.created_by for rule in rules)])
    rules = _settle_unreachable(plan, rules, recipients, threads)
    dropped_by = _apply_drops(plan, rules, by_thread, recipients)
    for rule in (rule for rule in rules if rule.action == DELIVER):
        thread = recipients[rule.id]
        if thread is None:
            continue  # _settle_unreachable dead-lettered it
        if thread in dropped_by:
            plan.settled.append(Outcome(
                rule.id, thread, DROPPED, reason=f"dropped by rule {dropped_by[thread]}"))
            continue
        routed_here = by_thread.get(thread)
        wake, reason, since_wake = _wake_for(
            db, rule, thread, threads[thread], routed_here, plan.lifecycle)
        target = plan.rides if routed_here else plan.adds
        delivery = target.setdefault(thread, Delivery(
            routed_wake=bool(routed_here and routed_here.disposition == "owner_wake")))
        delivery.add(rule, wake, reason, since_wake)
    return plan


def record(db: sqlite3.Connection, event: RuleEvent, outcomes: Iterable[Outcome]) -> None:
    """The one writer of rule outcomes; it also ages out rows past retention."""
    outcomes = list(outcomes)
    if not outcomes:
        return
    now = _now()
    ref = event.artifact_ref or {}
    db.executemany(
        "INSERT INTO event_route_decisions (at, event_id, connector, event_type, watch_event, "
        "artifact_integration, artifact_type, artifact_external_id, rule_id, thread_id, "
        "outcome, via, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (_iso(now), event.event_id, event.connector, event.event_type, event.watch_event,
             ref.get("integration"), ref.get("type"), ref.get("external_id"),
             item.rule_id, item.thread, item.outcome, item.via, item.reason)
            for item in outcomes
        ],
    )
    db.execute("DELETE FROM event_route_decisions WHERE at<?", (_iso(now - RETENTION),))
