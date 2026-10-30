"""Learning-status reports: strict payload validation and scope delivery.

A model-free producer supplies one report per scope and date through
:func:`deliver_learning_report_sync`. Each report has a ``report_key`` ending in
``daily``, ``early-1``, or ``early-2``; delivery is idempotent on that key.
The workspace scope resolves to the CORSIA agent and a project scope to that
project's designated conductor at delivery time. ``wake`` selects an actionable
wake; false records a passive digest note.

All top-level and nested object keys are required and exact. Count values are
``{"count": int >= 0, "refs": [non-empty record references...]}`` or
``null`` for not recorded. References are identifiers, not message text, and
are capped at 50 per count. Malformed reports raise before any delivery.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from events import report_payload
from events.connectors.measurement import LEARNING_REPORT_EVENT
from events.platform import PlatformDelivery
from events.report_delivery import deliver_measurement_report_sync

_REPORT_SUFFIXES = ("daily", "early-1", "early-2")
_LESSON_KEYS = ("recorded", "in_use", "confirmed", "consolidated", "repeated")
_EXPERIMENT_KEYS = ("started", "decided")
_MAX_REFS = 50


class MalformedLearningReport(ValueError):
    """A report that does not meet the payload contract; nothing is delivered."""


def _keys(raw: Any, required: set[str], what: str) -> Mapping[str, Any]:
    return report_payload.exact_keys(raw, required, what, MalformedLearningReport)


def _text(value: Any, what: str) -> str:
    return report_payload.text(value, what, MalformedLearningReport)


@dataclass(frozen=True)
class Count:
    """A recorded total and its bounded record references."""

    count: int
    refs: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any, what: str) -> Count | None:
        if raw is None:
            return None
        raw = _keys(raw, {"count", "refs"}, what)
        value = raw["count"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MalformedLearningReport(f"{what} count must be an integer >= 0")
        refs = raw["refs"]
        if not isinstance(refs, list) or len(refs) > _MAX_REFS:
            raise MalformedLearningReport(f"{what} refs must be a list of at most {_MAX_REFS}")
        return cls(value, tuple(_text(ref, f"{what} ref") for ref in refs))

    def as_payload(self) -> dict[str, Any]:
        return {"count": self.count, "refs": list(self.refs)}


def _counts(raw: Any, names: tuple[str, ...], what: str) -> dict[str, Count | None]:
    raw = _keys(raw, set(names), what)
    return {name: Count.parse(raw[name], f"{what} {name}") for name in names}


def _count_payload(value: Count | None) -> dict[str, Any] | None:
    return value.as_payload() if value is not None else None


@dataclass(frozen=True)
class LearningItems:
    """The report's four fixed groups of aggregate learning facts."""

    corrections: dict[str, Count | None]
    lessons: dict[str, Count | None]
    experiments: dict[str, Count | None]
    wins: Count | None

    @classmethod
    def parse(cls, raw: Any) -> LearningItems:
        raw = _keys(raw, {"corrections", "lessons", "experiments", "wins"}, "items")
        corrections_raw = raw["corrections"]
        if not isinstance(corrections_raw, Mapping):
            raise MalformedLearningReport("corrections must be an object")
        corrections = {
            _text(source, "correction source"): Count.parse(value, f"corrections {source}")
            for source, value in corrections_raw.items()
        }
        return cls(
            corrections,
            _counts(raw["lessons"], _LESSON_KEYS, "lessons"),
            _counts(raw["experiments"], _EXPERIMENT_KEYS, "experiments"),
            Count.parse(raw["wins"], "wins"),
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "corrections": {key: _count_payload(value) for key, value in self.corrections.items()},
            "lessons": {key: _count_payload(value) for key, value in self.lessons.items()},
            "experiments": {key: _count_payload(value) for key, value in self.experiments.items()},
            "wins": _count_payload(self.wins),
        }


@dataclass(frozen=True)
class LearningReport:
    """One daily or early learning-status report for one scope."""

    scope: str
    report_date: str
    report_key: str
    wake: bool
    items: LearningItems

    @classmethod
    def parse(cls, raw: Any) -> LearningReport:
        raw = _keys(raw, {"scope", "report_date", "report_key", "wake", "items"}, "report")
        scope = _text(raw["scope"], "scope")
        report_date = report_payload.report_date(raw["report_date"], MalformedLearningReport)
        report_key = _text(raw["report_key"], "report_key")
        allowed_keys = {f"{scope}:{report_date}:{suffix}" for suffix in _REPORT_SUFFIXES}
        if report_key not in allowed_keys:
            raise MalformedLearningReport(
                "report_key must match scope, report_date, and daily, early-1, or early-2")
        if not isinstance(raw["wake"], bool):
            raise MalformedLearningReport("wake must be true or false")
        return cls(scope, report_date, report_key, raw["wake"], LearningItems.parse(raw["items"]))

    def as_contract(self) -> dict[str, Any]:
        """The documented payload shape, the form :meth:`parse` accepts."""
        return {
            "scope": self.scope,
            "report_date": self.report_date,
            "report_key": self.report_key,
            "wake": self.wake,
            "items": self.items.as_payload(),
        }

    def as_event_payload(self) -> dict[str, Any]:
        return self.as_contract()


def validated(report: LearningReport | Mapping[str, Any]) -> LearningReport:
    """Parse a raw report, or re-validate a typed one built by hand."""
    return report_payload.revalidate(report, LearningReport)


def deliver_learning_report_sync(
    report: LearningReport | Mapping[str, Any], *, db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Validate, bind the scope's owner, and deliver one learning report."""
    parsed = validated(report)
    return deliver_measurement_report_sync(
        scope=parsed.scope, event_type=LEARNING_REPORT_EVENT,
        payload=parsed.as_event_payload(),
        transaction_scope="deliver_learning_report", db=db)
