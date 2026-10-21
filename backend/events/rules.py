"""Agent-written event rules: match one event, guard each recipient, record the outcome.

Today's routing decides first and stays in code (the webhook router's legs,
``resolve_targets``, the binder, the self-authored and cancelled-check drops).
A rule an agent wrote only adds to that decision:

- a ``deliver`` rule adds a recipient, or rides a delivery routing already made
  to that thread (its note joins it, and a wake beats a digest);
- a ``drop`` rule removes its writer's own delivery, nothing else.

Every outcome of a rule goes through :func:`record`, the one writer of
``event_route_decisions``. With no enabled rule for an event's type, evaluation
costs one indexed read and nothing else. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from events import rule_reads

DELIVER = "deliver"
DROP = "drop"
WAKE = "wake"
DIGEST = "digest"
REGISTERED_OWNER = "registered_owner"
THREAD_PREFIX = "thread:"
PROJECT_PREFIX = "project:"
OPERATORS = ("eq", "not_eq", "in", "prefix", "present")
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
LOCAL_ONLY = "local only"
ACCOUNT_REACH = "account reach"

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
class Routed:
    """One delivery today's routing chose: its thread and whether it wakes."""

    thread: str
    wake: bool
    owner: bool = False


@dataclass(frozen=True)
class Entry:
    """One rule's part in a delivery: whether it asks to wake, and why not."""

    rule_id: str
    wake: bool
    reason: str | None = None  # R7 or R9 turned its wake into a digest
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


def lookup(db: sqlite3.Connection, connector: str, event_type: str) -> list[Rule]:
    """The enabled rules for one declared type: the one read every event pays."""
    rows = db.execute(
        "SELECT * FROM event_rules WHERE connector=? AND event_type=? AND enabled=1 "
        "ORDER BY created_at, id",
        (connector, event_type),
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


def _wake_budget(db: sqlite3.Connection, rule_id: str, thread: str) -> tuple[bool, int]:
    """R9: whether this rule already woke this thread within the hour, and how many
    of its events the cap has digested there since its last wake, in one read."""
    row = db.execute(
        "SELECT (SELECT COUNT(*) FROM event_route_decisions WHERE rule_id=:rule "
        "        AND thread_id=:thread AND outcome='woke' AND via='rule' AND at>=:since) AS spent, "
        "       (SELECT COUNT(*) FROM event_route_decisions WHERE rule_id=:rule "
        "        AND thread_id=:thread AND outcome='digested' AND reason=:cap "
        "        AND id>COALESCE((SELECT MAX(id) FROM event_route_decisions WHERE rule_id=:rule "
        "                AND thread_id=:thread AND outcome='woke' AND via='rule'), 0)) AS capped",
        {"rule": rule_id, "thread": thread, "cap": WAKE_CAP,
         "since": _iso(_now() - timedelta(hours=1))},
    ).fetchone()
    return int(row["spent"]) >= WAKES_PER_HOUR, int(row["capped"])


def _in_scope(rule: Rule, event_project: str | None) -> bool:
    """An event with a project matches only that project's rules. One with no owner
    and nothing routed has no project, so only a rule delivering to its own writer
    sees it: that writer already reads the source, and no one else is reached."""
    if event_project is not None:
        return rule.scope == project_scope(event_project)
    return rule.action == DELIVER and thread_target(rule.to) == rule.created_by


def _paused(db: sqlite3.Connection, thread: str) -> bool:
    from plans.public import thread_paused_since

    return thread_paused_since(db, thread) is not None


def _reaches(db: sqlite3.Connection, account_id: Any, thread: str) -> bool:
    from connections.connected_accounts import account_reaches_thread

    return account_reaches_thread(str(account_id), thread, db=db)


def _apply_drops(plan: RulePlan, rules: list[Rule], by_thread: Mapping[str, Routed]) -> dict[str, str]:
    """Each drop rule removes its writer's own routed delivery; returns writer -> rule."""
    dropped_by: dict[str, str] = {}
    for rule in (rule for rule in rules if rule.action == DROP):
        writer = rule.created_by
        dropped_by.setdefault(writer, rule.id)
        if writer in by_thread:
            plan.drops.setdefault(writer, rule.id)
        plan.settled.append(Outcome(
            rule.id, writer, DROPPED,
            reason=None if writer in by_thread else "nothing routed to drop"))
    return dropped_by


def _wake_for(
    db: sqlite3.Connection, rule: Rule, thread: str, routed_wake: bool,
) -> tuple[bool, str | None, int]:
    """Whether this rule wakes the thread, why not (R7, R9), and its capped count."""
    if rule.mode != WAKE:
        return False, None, 0
    if routed_wake:
        return False, None, 0  # routing already wakes it; the rule only rides
    if _paused(db, thread):
        return False, PAUSED, 0  # R7
    spent, capped = _wake_budget(db, rule.id, thread)
    if spent:
        return False, WAKE_CAP, capped + 1  # R9
    return True, None, 0


def evaluate(
    db: sqlite3.Connection, event: RuleEvent, routed: Iterable[Routed],
) -> RulePlan | None:
    """Decide what agent-written rules add to today's routing for one event.

    Returns ``None`` when no enabled rule for the event's type matches its facts;
    the caller then delivers exactly as it would without rules.
    """
    rules = [rule for rule in lookup(db, event.connector, event.event_type)
             if matches(rule, event.facts)]
    if not rules:
        return None
    routed = list(routed)
    by_thread = {item.thread: item for item in routed}
    owner, ended = rule_reads.artifact_context(db, event.artifact_ref)
    owner = owner or next((item.thread for item in routed if item.owner), None)
    recipients = {
        rule.id: (owner if rule.to == REGISTERED_OWNER else thread_target(rule.to))
        for rule in rules
    }
    threads = rule_reads.threads(db, [owner, *by_thread, *recipients.values()])
    event_project = threads[owner]["project_id"] if owner in threads else None
    if event_project is None:
        event_project = next(
            (threads[item.thread]["project_id"] for item in routed if item.thread in threads), None)
    account_id = event.facts.get("account_id")
    if account_id is None:
        rules = [rule for rule in rules if _in_scope(rule, event_project)]
    plan = RulePlan(event)
    if not rules:
        return plan
    if ended and not event.ends_artifact:
        # R27: an ended artifact's later activity fires no rule.
        plan.settled = [Outcome(rule.id, recipients[rule.id], ENDED) for rule in rules]
        return plan

    dropped_by = _apply_drops(plan, rules, by_thread)
    cloud_off: set[str] | None = None
    for rule in (rule for rule in rules if rule.action == DELIVER):
        thread = recipients[rule.id]
        row = threads.get(thread) if thread else None
        if thread is None or row is None or not row["available"]:
            reason = "no registered owner" if thread is None else "recipient unavailable"
            plan.settled.append(Outcome(rule.id, thread, DEAD_LETTERED, reason=reason))
            continue
        if thread in dropped_by:
            plan.settled.append(Outcome(
                rule.id, thread, DROPPED, reason=f"dropped by rule {dropped_by[thread]}"))
            continue
        if account_id is not None and not _reaches(db, account_id, thread):
            # R30: the account this event came from must reach the recipient.
            plan.settled.append(Outcome(rule.id, thread, HELD, reason=ACCOUNT_REACH))
            continue
        if cloud_off is None:
            cloud_off = rule_reads.cloud_off(
                db, [event_project, *(r["project_id"] for r in threads.values())])
        if (event_project in cloud_off and not row["local_only"]
                and row["project_id"] not in cloud_off):
            # R29: a Local-models-only project's event reaches no cloud recipient.
            plan.settled.append(Outcome(rule.id, thread, HELD, reason=LOCAL_ONLY))
            continue
        routed_here = by_thread.get(thread)
        routed_wake = bool(routed_here and routed_here.wake)
        wake, reason, since_wake = _wake_for(db, rule, thread, routed_wake)
        target = plan.rides if routed_here else plan.adds
        delivery = target.setdefault(thread, Delivery(routed_wake=routed_wake))
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
