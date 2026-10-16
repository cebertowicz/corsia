"""The daily movement report: typed payload, validator, and routing to its owner.

A report producer (Self-Improvement's, which reads the dashboard series) hands
the platform one report per scope per day through :func:`deliver_movement_report`.
Nothing else is needed to route it. The report goes to the scope's conductor
as a passive digest note, or as a wake when ``breach`` is true. The workspace
scope reaches the CORSIA agent; a project scope reaches that project's
conductor. The owner is resolved from the current designation at delivery
time (a designation rule), never from a stored thread id, so a designation
move reaches the new owner with the next report.

Payload contract (all keys required; any other shape raises
:class:`MalformedMovementReport`)::

    {
      "scope": "workspace" | "<project id>",
      "report_date": "YYYY-MM-DD",
      "breach": bool,        # true only when a person-attention or cost series
                             # crossed its threshold; wakes the owner
      "moves": [             # empty on a no-move day
        {
          "series_id": str,
          "owner_agent": str,          # thread id of the series' owner
          "value": number,             # the reading that moved
          "normal_range": [low, high], # the series' own range, low <= high
          "preceding_changes": [       # changes in the window before the move
            {"kind": "instruction"|"skill"|"workflow"|"model"|"roster",
             "ref": str,               # path or id
             "at": "<ISO-8601 timestamp>"}
          ]
        }
      ]
    }

``preceding_changes`` records what happened before a move. It is never a
cause. A report is one event per scope per day (idempotent on that key), so a
repeat delivery is a no-op.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from connections import binder
from events.connectors.measurement import MEASUREMENT_CONNECTOR
from events.platform import PlatformDelivery, deliver_platform_event_sync
from infra.database import write_transaction
from infra.db_async import run_in_db_unless_owned

CHANGE_KINDS = ("instruction", "skill", "workflow", "model", "roster")


class MalformedMovementReport(ValueError):
    """A report that does not meet the payload contract; nothing is delivered."""


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MalformedMovementReport(f"{what} must be a non-empty string")
    return value


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedMovementReport(f"{what} must be a number")
    return value


def _keys(raw: Any, required: set[str], what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise MalformedMovementReport(f"{what} must be an object")
    missing, extra = required - set(raw), set(raw) - required
    if missing or extra:
        raise MalformedMovementReport(
            f"{what}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    return raw


@dataclass(frozen=True)
class PrecedingChange:
    """One journaled change that happened before a move (never a cause)."""

    kind: str
    ref: str
    at: str

    @classmethod
    def parse(cls, raw: Any) -> PrecedingChange:
        raw = _keys(raw, {"kind", "ref", "at"}, "preceding change")
        kind = _text(raw["kind"], "change kind")
        if kind not in CHANGE_KINDS:
            raise MalformedMovementReport(
                f"change kind {kind!r} not one of {list(CHANGE_KINDS)}")
        at = _text(raw["at"], "change timestamp")
        try:
            datetime.fromisoformat(at)
        except ValueError as exc:
            raise MalformedMovementReport(
                f"change timestamp {at!r} is not ISO-8601") from exc
        return cls(kind, _text(raw["ref"], "change ref"), at)


@dataclass(frozen=True)
class Move:
    """One series that moved outside its own normal range."""

    series_id: str
    owner_agent: str
    value: float
    normal_range: tuple[float, float]
    preceding_changes: tuple[PrecedingChange, ...]

    @classmethod
    def parse(cls, raw: Any) -> Move:
        raw = _keys(raw, {"series_id", "owner_agent", "value", "normal_range",
                          "preceding_changes"}, "move")
        bounds = raw["normal_range"]
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise MalformedMovementReport("normal_range must be [low, high]")
        low = _number(bounds[0], "normal_range low")
        high = _number(bounds[1], "normal_range high")
        if low > high:
            raise MalformedMovementReport("normal_range low exceeds high")
        changes = raw["preceding_changes"]
        if not isinstance(changes, (list, tuple)):
            raise MalformedMovementReport("preceding_changes must be a list")
        return cls(
            _text(raw["series_id"], "series_id"),
            _text(raw["owner_agent"], "owner_agent"),
            _number(raw["value"], "value"), (low, high),
            tuple(PrecedingChange.parse(c) for c in changes),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id, "owner_agent": self.owner_agent,
            "value": self.value, "normal_range": list(self.normal_range),
            "preceding_changes": [
                {"kind": c.kind, "ref": c.ref, "at": c.at}
                for c in self.preceding_changes],
        }


@dataclass(frozen=True)
class MovementReport:
    """One scope's report for one day."""

    scope: str
    report_date: str
    breach: bool
    moves: tuple[Move, ...]

    @classmethod
    def parse(cls, raw: Any) -> MovementReport:
        raw = _keys(raw, {"scope", "report_date", "breach", "moves"}, "report")
        day = _text(raw["report_date"], "report_date")
        try:
            date.fromisoformat(day)
        except ValueError as exc:
            raise MalformedMovementReport(
                f"report_date {day!r} is not YYYY-MM-DD") from exc
        if not isinstance(raw["breach"], bool):
            raise MalformedMovementReport("breach must be true or false")
        if not isinstance(raw["moves"], (list, tuple)):
            raise MalformedMovementReport("moves must be a list")
        moves = tuple(Move.parse(m) for m in raw["moves"])
        if raw["breach"] and not moves:
            raise MalformedMovementReport("a breach report needs at least one move")
        return cls(_text(raw["scope"], "scope"), day, raw["breach"], moves)

    @property
    def report_key(self) -> str:
        return f"{self.scope}:{self.report_date}"

    def as_contract(self) -> dict[str, Any]:
        """The documented payload shape, the form :meth:`parse` accepts."""
        return {
            "scope": self.scope, "report_date": self.report_date,
            "breach": self.breach, "moves": [m.as_payload() for m in self.moves],
        }

    def as_event_payload(self) -> dict[str, Any]:
        return {**self.as_contract(), "report_key": self.report_key}


def validated(report: MovementReport | Mapping[str, Any]) -> MovementReport:
    """Parse a raw report, or re-validate a typed one built by hand."""
    raw = report.as_contract() if isinstance(report, MovementReport) else report
    return MovementReport.parse(raw)


def deliver_movement_report_sync(
    report: MovementReport | Mapping[str, Any], *, db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Validate, bind the scope's conductor, and deliver one report."""
    parsed = validated(report)
    with write_transaction(db, scope="deliver_movement_report") as conn:
        binder.ensure_designation_rule(
            connector=MEASUREMENT_CONNECTOR, fact="scope", value=parsed.scope,
            designation=parsed.scope, db=conn)
        return deliver_platform_event_sync(
            MEASUREMENT_CONNECTOR, parsed.as_event_payload(), db=conn)


async def deliver_movement_report(
    report: MovementReport | Mapping[str, Any], *, db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """The producer's one call. Raises :class:`MalformedMovementReport` first."""
    parsed = validated(report)
    return await run_in_db_unless_owned(
        db, deliver_movement_report_sync, parsed, db=db)
