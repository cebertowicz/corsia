"""Agent-written event rules: match one event, guard each recipient, record the outcome.

Today's routing decides first and stays in code (the webhook router's legs,
``resolve_targets``, the binder, the self-authored and cancelled-check drops).
Rules read that decision (its ``Target`` list, the owner and lifecycle it
resolved) and never re-derive it. A rule an agent wrote only adds to it:

- a ``deliver`` rule adds a recipient, or rides a delivery routing already made
  to that thread (its note joins it, and a wake beats a digest);
- a ``drop`` rule removes its writer's own delivery, nothing else;
- a ``run_script`` rule queues one run of its writer's script and changes no
  delivery (``events.rule_runs``);
- a ``start_worker`` rule queues one worker start per request and changes no
  delivery (``events.worker_starts``).

Every outcome of a rule goes through :func:`record`, the one writer of
``event_route_decisions`` rows; ``events.rule_runs`` only advances a queued
run's row. With no enabled rule for an event's type in the
event's project, evaluation costs one indexed read and nothing else. Whether a
rule's wake ask wakes is the disposition table's answer. Names no connector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from events import rule_reads, worker_starts
from events.rule_match import matches, repository_fact
from events.disposition import WAKE as TABLE_WAKE, disposition
from events.resolve import Target, lifecycle
from events.rule_outcomes import (  # noqa: F401 - re-exported: callers read them as rules.*
    DEAD_LETTERED, DELIVER, DIGESTED, DROP, DROPPED, DUPLICATE, ENDED, HELD, RUN_SCRIPT, START_WORKER,
    WOKE, Delivery, Entry, Outcome, Rule, RuleEvent, RulePlan,
)
from events.rule_runs import OWNER_GONE, queue
from infra.queries import thread_gone_for_good, thread_is_temporary, thread_unavailable_reason
from registry.retirement_predicate import identity_ended

ROUTE = "route"  # a rule's kind; the other is observe (``events.observe_rules``)
WAKE = "wake"
DIGEST = "digest"
REGISTERED_OWNER = "registered_owner"
THREAD_PREFIX = "thread:"
PROJECT_PREFIX = "project:"
OPERATORS = ("eq", "not_eq", "in", "prefix", "present")
NAMING = ("eq", "in")  # the operators whose values name what an event must be
MAX_TESTS = 4
NOTE_LIMIT = 500

# Reasons a guard gives.
PAUSED = "paused"
NEVER_WAKES = "this recipient never wakes for this artifact"
OTHER_OWNER = "the registered owner is another agent"
OUT_OF_PROJECT = "recipient outside the rule's project"
LOOKUP_FAILED = "repository lookup failed"
UNAVAILABLE = "recipient unavailable"
WRITER_LEFT = "its writer left the rule's project; rule turned off"
CREATOR_GONE = "creator gone"
CREATOR_UNAVAILABLE = "creator unavailable"
OWN_EMIT = "sent by this chat"

RETENTION = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def from_row(row: Mapping[str, Any]) -> Rule:
    return Rule(
        id=row["id"], scope=row["scope"], connector=row["connector"],
        event_type=row["event_type"], match=tuple(json.loads(row["match"])),
        action=row["action"], to=row["to"], mode=row["mode"], note=row["note"],
        created_by=row["created_by"], enabled=bool(row["enabled"]), script_id=row["script_id"],
        workflow_id=row["workflow_id"], request_identity_fact=row["request_identity_fact"],
    )


def lookup(
    db: sqlite3.Connection, connector: str, event_type: str, thread: str | None,
    projects: Iterable[str] = (),
) -> list[Rule]:
    """The enabled rules for one declared type in ``thread``'s project (rules resolve
    in the artifact owner's scope), or, with no thread, in each of ``projects``: the
    one indexed read every event pays."""
    rows = rule_reads.enabled_rules(db, connector, event_type, PROJECT_PREFIX, thread,
                                    [project_scope(project) for project in projects])
    return [from_row(row) for row in rows]


def reaches_unowned(rule: Rule, event: RuleEvent) -> bool:
    """Whether ``rule`` fires on an event routing gave no thread: one CORSIA produced for a
    requester (a worker's result) reaches only that requester's rules to itself; any other
    reaches only :func:`self_on_repository`'s."""
    if event.requester is None:
        return self_on_repository(rule)
    return (rule.created_by == event.requester and rule.action != DROP
            and thread_target(rule.to) == rule.created_by)


def self_on_repository(rule: Rule) -> bool:
    """An event with no owner and nothing routed reaches only a writer's rule to
    itself (a delivery or its own script) that names the event's repository: create
    checked that the writer's project tracks that repository, and no one else is reached."""
    if rule.action == DROP or thread_target(rule.to) != rule.created_by:
        return False
    fact = repository_fact(rule.connector)
    return fact is not None and any(
        test["field"] == fact and test["op"] in NAMING for test in rule.match)


def lookup_failed(db: sqlite3.Connection, event: RuleEvent) -> list[Outcome]:
    """Fail closed, as ``create`` does: no self-rule fires on an event routing gave no
    thread when the projects tracking its repository can't be read; each that matched
    is dead-lettered with the reason."""
    repository = event.facts.get(repository_fact(event.connector) or "")
    logger.warning("event rules: repository lookup failed for {} {} on {}; no self-rule fired",
                   event.connector, event.event_type, repository)
    return [
        Outcome(rule.id, rule.created_by, DEAD_LETTERED, reason=LOOKUP_FAILED)
        for rule in map(from_row, rule_reads.self_rules(db, event.connector, event.event_type))
        if matches(rule, event.facts) and self_on_repository(rule)
    ]


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


def _apply_drops(
    plan: RulePlan, rules: list[Rule], by_thread: Mapping[str, Target],
    recipients: Mapping[str, str | None],
) -> dict[str, str]:
    """Each drop rule removes its writer's own delivery, routed or rule-added, and
    records an outcome only when it removed one (a script run counts); returns writer -> rule."""
    added = {recipients[rule.id] for rule in rules if rule.action != DROP}
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
    db: sqlite3.Connection, plan: RulePlan, rules: list[Rule],
    recipients: Mapping[str, str | None], threads: Mapping[str, Mapping[str, Any]],
) -> list[Rule]:
    """Settle each rule whose affected thread (a delivery's recipient, a drop's
    writer) is unavailable or no longer in the rule's project; return the rest.
    One whose writer left its project is turned off with ``disable``'s write."""
    current = []
    for rule in rules:
        thread = recipients[rule.id] if rule.action == DELIVER else rule.created_by
        row = threads.get(thread) if thread else None
        writer = threads.get(rule.created_by)
        if thread_gone_for_good(writer):
            turn_off(db, rule.id, rule.scope, _iso(_now()))
            plan.settled.append(Outcome(rule.id, rule.created_by, DEAD_LETTERED, reason=CREATOR_GONE))
        elif thread_unavailable_reason(writer):
            plan.settled.append(Outcome(rule.id, rule.created_by, HELD, reason=CREATOR_UNAVAILABLE))
        elif writer is not None and writer["project_id"] != rule.scope.removeprefix(PROJECT_PREFIX):
            turn_off(db, rule.id, rule.scope, _iso(_now()))
            plan.settled.append(Outcome(rule.id, rule.created_by, DEAD_LETTERED,
                                        reason=WRITER_LEFT))
        elif rule.action != DROP and (row is None or not row["available"]):
            reason = ("no registered owner" if thread is None
                      else UNAVAILABLE if rule.action == DELIVER else OWNER_GONE)
            plan.settled.append(Outcome(rule.id, thread, DEAD_LETTERED, reason=reason))
        elif row is not None and row["project_id"] != rule.scope.removeprefix(PROJECT_PREFIX):
            plan.settled.append(Outcome(rule.id, thread, HELD, reason=OUT_OF_PROJECT))
        else:
            current.append(rule)
    return current


def turn_off(db: sqlite3.Connection, rule_id: str, scope: str, now: str) -> None:
    """Disable one rule and prune its scope's stale disabled rules: ``disable``'s
    write, and evaluation's for a rule whose writer left its project."""
    db.execute("UPDATE event_rules SET enabled=0, updated_at=? WHERE id=?", (now, rule_id))
    prune_disabled(db, scope, now)


def prune_disabled(db: sqlite3.Connection, scope: str, now: str) -> None:
    """Keep disabled rules briefly for inspection, then discard them, and what
    they did, in their own scope."""
    cutoff = (datetime.fromisoformat(now) - RETENTION).isoformat()
    stale = "SELECT id FROM event_rules WHERE scope=? AND enabled=0 AND updated_at<?"
    db.execute(f"DELETE FROM event_route_decisions WHERE rule_id IN ({stale})", (scope, cutoff))
    db.execute(f"DELETE FROM event_rules WHERE id IN ({stale})", (scope, cutoff))


def _wake_guard(
    rule: Rule, thread: str, row: Mapping[str, Any], routed_here: Target | None,
    artifact_lifecycle: str,
) -> tuple[bool, str | None] | None:
    """Why this rule can't wake the thread, or ``None`` while it still asks.

    The disposition table answers whether this recipient can wake at all; 4.4:
    an agent's ``registered_owner`` rule wakes only itself or its temporary workers.
    """
    if rule.mode != WAKE:
        return False, None
    if routed_here is not None and routed_here.disposition == "owner_wake":
        return False, None  # routing already wakes it; the rule only rides
    relationship = routed_here.relationship if routed_here is not None else "rule"
    if routed_here is not None and routed_here.routing_state:
        artifact_lifecycle = lifecycle(routed_here.routing_state)
    if disposition(relationship, "informational", artifact_lifecycle,
                   rule_wake=True) != TABLE_WAKE:
        return False, NEVER_WAKES
    if (rule.to == REGISTERED_OWNER and thread != rule.created_by
            and (row["spawned_by"] != rule.created_by or not thread_is_temporary(row))):
        return False, OTHER_OWNER  # 4.4
    return None


def _wakes(
    db: sqlite3.Connection, asking: Mapping[str, list[str]],
) -> dict[str, tuple[bool, str | None]]:
    """Apply the paused-plan decision once per recipient thread."""
    answers: dict[str, tuple[bool, str | None]] = {}
    for thread, rule_ids in asking.items():
        answer = (False, PAUSED) if rule_reads.paused(db, thread) else (True, None)
        answers.update({rule_id: answer for rule_id in rule_ids})
    return answers


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
    scope = anchor or event.requester
    rules = [rule for rule in lookup(db, event.connector, event.event_type, scope,
                                     () if scope else event.repository_projects)
             if matches(rule, event.facts) and (anchor or reaches_unowned(rule, event))]
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
    rules = _settle_unreachable(db, plan, rules, recipients, threads)
    dropped_by = _apply_drops(plan, rules, by_thread, recipients)
    plan.settled += queue(db, event, rules, dropped_by)
    plan.settled += worker_starts.queue(db, event, rules, dropped_by)
    delivering: list[tuple[Rule, str, Target | None]] = []
    answers: dict[str, tuple[bool, str | None]] = {}
    asking: dict[str, list[str]] = {}
    for rule in (rule for rule in rules if rule.action == DELIVER):
        thread = recipients[rule.id]
        if thread is None:
            continue  # _settle_unreachable dead-lettered it
        if thread in dropped_by:
            plan.settled.append(Outcome(
                rule.id, thread, DROPPED, reason=f"dropped by rule {dropped_by[thread]}"))
            continue
        delivering.append((rule, thread, by_thread.get(thread)))
        answer = _wake_guard(rule, thread, threads[thread], by_thread.get(thread), plan.lifecycle)
        if answer is None:
            asking.setdefault(thread, []).append(rule.id)
        else:
            answers[rule.id] = answer
    answers.update(_wakes(db, asking))
    for rule, thread, routed_here in delivering:
        target = plan.rides if routed_here else plan.adds
        delivery = target.setdefault(thread, Delivery(
            routed_wake=bool(routed_here and routed_here.disposition == "owner_wake")))
        delivery.add(rule, *answers[rule.id])
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
        "outcome, via, reason, run_input, request_identity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (_iso(now), event.event_id, event.connector, event.event_type, event.watch_event,
             ref.get("integration"), ref.get("type"), ref.get("external_id"),
             item.rule_id, item.thread, item.outcome, item.via, item.reason, item.run_input,
             item.request_identity)
            for item in outcomes
        ],
    )
    cutoff = _iso(now - RETENTION)
    db.execute("DELETE FROM event_route_decisions WHERE at<?", (cutoff,))
