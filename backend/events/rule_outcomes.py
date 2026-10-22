"""What agent-written rules decided for one event: each rule's outcome, and the deliveries they make.

``events.rules`` decides, ``events.platform`` delivers, and ``rules.record``
writes the outcome rows. A ``run_script`` rule's row is also its run's queue
(``events.rule_runs``). Names no connector.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from events.rules import Rule

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

WAKE_CAP = "wake cap"  # the reason R9's hourly cap turned a rule's wake into a digest


@dataclass(frozen=True)
class Entry:
    """One rule's part in a delivery: whether it asks to wake, and why not."""

    rule_id: str
    wake: bool
    reason: str | None = None  # why its wake ask became a digest (R7, R9, 4.4, the table)
    since_wake: int = 0  # R9: this rule's capped digests to the thread since it last woke it


@dataclass(frozen=True)
class Outcome:
    rule_id: str
    thread: str | None
    outcome: str
    via: str = "rule"  # "routed": the rule rode a delivery something else made
    reason: str | None = None
    run_input: str | None = None  # a queued run's input, until the runner claims it


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
