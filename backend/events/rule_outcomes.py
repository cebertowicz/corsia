"""What agent-written rules are, and what they decided for one event: each rule's outcome and deliveries.

``events.rules`` decides, ``events.platform`` delivers, and ``rules.record``
writes the outcome rows. A ``run_script`` rule's row is also its run's queue
(``events.rule_runs``), and a ``start_worker`` rule's row its start's
(``events.worker_starts``). The shapes live here so those modules and
``events.rules`` read them without importing each other. Names no connector.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Actions a rule takes.
DELIVER = "deliver"
DROP = "drop"
RUN_SCRIPT = "run_script"
START_WORKER = "start_worker"

# Outcomes, as event_route_decisions stores them.
WOKE = "woke"
DIGESTED = "digested"
DROPPED = "dropped"
HELD = "held"
DEAD_LETTERED = "dead_lettered"
ENDED = "ended"
DUPLICATE = "duplicate"
# A run_script rule's run: queued by evaluation, then running, ran or failed; or skipped.
QUEUED = "queued"
RUNNING = "running"
RAN = "ran"
FAILED = "failed"
SKIPPED = "skipped"
# A start_worker rule's start: queued by evaluation (and while the worker limit is full),
# then started, and settled once by its worker's result (returned, invalid or no_result);
# or skipped, or refused before any worker was made.
STARTED = "started"
RETURNED = "returned"
INVALID = "invalid"
NO_RESULT = "no_result"
REFUSED = "refused"

# An observe rule's outcome: the caller was registered to what its tool call created, or not.
REGISTERED = "registered"
NOT_REGISTERED = "not_registered"

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
    script_id: str | None = None  # run_script: the writer's script schedule it runs
    workflow_id: str | None = None  # start_worker: the workflow its worker runs
    request_identity_fact: str | None = None  # start_worker: the fact naming one request (R24)


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
    # Read only when routing gave the event no thread: the projects tracking its repository.
    repository_projects: frozenset[str] = frozenset()
    trace: tuple[str, ...] = ()  # the rules whose script runs led to this event, oldest first
    # The agent whose rule started the worker this event reports on, set by the server only:
    # with no owner to scope by, the event still reaches that agent's own rules.
    requester: str | None = None


@dataclass(frozen=True)
class Entry:
    """One rule's part in a delivery: whether it asks to wake, and why not."""

    rule_id: str
    wake: bool
    reason: str | None = None  # why its wake ask became a digest


@dataclass(frozen=True)
class Outcome:
    rule_id: str
    thread: str | None
    outcome: str
    via: str = "rule"  # "routed": the rule rode a delivery something else made
    reason: str | None = None
    run_input: str | None = None  # a queued run's input, until the runner claims it
    request_identity: str | None = None  # a worker start's request (R24)


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

    def add(self, rule: Rule, wake: bool, reason: str | None) -> None:
        self.entries.append(Entry(rule.id, wake, reason))
        if rule.note:
            self.notes.append(rule.note)

    def outcomes(self, thread: str, delivered: str, *, ride: bool) -> list[Outcome]:
        """Each rule's outcome once the delivery is made (woke, digested or duplicate).

        ``via='rule'`` marks the rule that caused the wake or digest; a rule that
        rode one something else caused is ``via='routed'``.
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


@dataclass
class RulePlan:
    """Everything rules decided for one event, before delivery."""

    event: RuleEvent
    lifecycle: str = "active"  # the artifact's, as routing resolved it
    drops: dict[str, str] = field(default_factory=dict)       # routed thread -> drop rule id
    rides: dict[str, Delivery] = field(default_factory=dict)  # routed thread -> what rides it
    adds: dict[str, Delivery] = field(default_factory=dict)   # new thread -> its delivery
    settled: list[Outcome] = field(default_factory=list)      # outcomes with no delivery

    def handled_by(self) -> str | None:
        """A rule that queued a run of its writer's script, or a worker start, with this event."""
        return next((item.rule_id for item in self.settled if item.outcome == QUEUED), None)
