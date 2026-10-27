"""Self-caused source event identities in the rule outcome retention record."""
from __future__ import annotations

from events import rules
from events.rule_outcomes import DROPPED, REGISTERED, Outcome, RuleEvent
from infra.database import write_transaction

_MARKER = "observe:self_caused"
_REASON = "self_caused"


def identity(connector: str, kind: str, source_id: str) -> str:
    return f"self_caused:{connector}:{kind}:{source_id}"


def record(db, thread: str, rule, events: tuple[tuple[str, str], ...]) -> None:
    if not events:
        return
    with write_transaction(db, scope="observe_self_caused") as conn:
        for kind, source_id in events:
            event = RuleEvent(identity(rule.connector, kind, source_id), rule.connector, _MARKER, "", {}, None)
            rules.record(conn, event, [Outcome(rule.id, thread, REGISTERED, reason=_REASON)])


def suppress(connector: str, kind: str, source_id: str, thread: str, artifact_ref=None) -> bool:
    event_id = identity(connector, kind, source_id)
    with write_transaction(None, scope="self_caused_route") as db:
        row = db.execute(
            "SELECT rule_id FROM event_route_decisions WHERE event_id=? AND event_type=? "
            "AND thread_id=? AND at>=? LIMIT 1",
            (event_id, _MARKER, thread, rules._iso(rules._now() - rules.RETENTION)),
        ).fetchone()
        if row is None:
            return False
        event = RuleEvent(event_id, connector, kind, "", {}, artifact_ref)
        rules.record(db, event, [Outcome(row["rule_id"], thread, DROPPED, reason=_REASON)])
        return True
