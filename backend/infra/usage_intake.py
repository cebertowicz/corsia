"""Intake of install usage summaries on our own instance.

The pull from the store-and-forward endpoint (not built here) hands each fetched body to
``admit_summary``; this instance never takes the connection itself. A summary is checked
against the closed schema in ``core.usage_fields`` (the same list the allowlist in
``infra.outcome_event_attributes`` admits), a refusal is counted in the shared
``record_refused`` counter, and an admitted one adds to a total and is then forgotten:
no summary's own row is kept.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.usage_fields import USAGE_EVENT
from infra.database import write_transaction
from infra.outcome_events import count_outcome_event
from infra.usage_summary import SummaryError, validate_summary

_ADD = (
    "INSERT INTO usage_install_totals (week,release_tag,os,chip,install_weeks) VALUES (?,?,?,?,1) "
    "ON CONFLICT(week,release_tag,os,chip) DO UPDATE SET install_weeks=install_weeks+1"
)


@dataclass(frozen=True)
class SummaryAdmission:
    accepted: bool
    refusal: str | None = None


@dataclass(frozen=True)
class BatchAdmission:
    accepted: int
    refused: int


def admit_summary(db: sqlite3.Connection, body: Any) -> SummaryAdmission:
    """Admit one fetched summary into the totals, or count and explain its refusal."""
    try:
        summary = validate_summary(body)
    except SummaryError as exc:
        with write_transaction(db, scope="refuse_usage_summary") as connection:
            count_outcome_event(connection, "record_refused", USAGE_EVENT)
        return SummaryAdmission(False, f"{USAGE_EVENT}: {exc}; the summary was dropped.")
    with write_transaction(db, scope="admit_usage_summary") as connection:
        connection.execute(_ADD, (summary["week"], summary["release"], summary.get("os", ""), summary.get("chip", "")))
    return SummaryAdmission(True)


def admit_summaries(db: sqlite3.Connection, bodies: Iterable[Any]) -> BatchAdmission:
    """Admit a pulled batch, one summary at a time, so one bad body never blocks the rest."""
    accepted = refused = 0
    for body in bodies:
        if admit_summary(db, body).accepted:
            accepted += 1
        else:
            refused += 1
    return BatchAdmission(accepted, refused)
