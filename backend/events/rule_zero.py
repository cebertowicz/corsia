"""When rule 0 adds no delivery to an event's registered owner.

Rule 0 is today's routing (``resolve_targets``); it is off for an event in two cases, and
only the rules that name a recipient deliver it then:

* its declared type says ``default_delivery: none`` (the declaring rule set delivers it);
* a worker's result or no-result that the rule owner's own ``run_script`` rule took, so the
  script is the notification and the item's owner is not also sent the raw result (R24).

A recipient a rule rides keeps its delivery: that rule asked for it. Names no connector.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from events import rule_runs
from events.connectors import EventTypeDecl
from events.resolve import Target
from events.rule_outcomes import RuleEvent, RulePlan

HELD = "rule 0 is off for this event; its rules deliver it"


def off(db: sqlite3.Connection, decl: EventTypeDecl, event: RuleEvent) -> bool:
    """Whether rule 0 delivers nothing for this event; read once its rule outcomes are recorded."""
    return decl.default_delivery == "none" or (
        event.requester is not None and rule_runs.took(db, event.event_id, event.requester))


def hold(
    db: sqlite3.Connection, decl: EventTypeDecl, event: RuleEvent, plan: RulePlan | None,
    pairs: Sequence[tuple[Target, str | None]],
) -> list[tuple[Target, str | None]]:
    """``pairs`` (routed target, the caller's filter reason) with each rule 0 delivery held
    when rule 0 is off, except one a rule rides."""
    if not off(db, decl, event):
        return list(pairs)
    rides = plan.rides if plan is not None else {}
    return [(target, reason or (None if target.thread in rides else HELD))
            for target, reason in pairs]
