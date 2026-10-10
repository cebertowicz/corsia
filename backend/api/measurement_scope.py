"""HTTP compatibility facade for canonical Measurement filters and windows."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import HTTPException

from core import measurement_scope as scope
from core.measurement_scope import Grain, MeasurementScope, query_params, scope_cte, usage_bounds


def validate_scope(db, user_id: str, project: str, agent: str | None) -> MeasurementScope:
    try:
        return scope.validate_scope(db, user_id, project, agent)
    except scope.ScopeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def parse_trend_range(value: str) -> str:
    try:
        return scope.parse_trend_range(value)
    except scope.ScopeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def completed_week_bounds(value: str, earliest: date | None = None, *,
                          now: datetime | None = None) -> tuple[date, date]:
    try:
        return scope.completed_week_bounds(value, earliest, now=now)
    except scope.ScopeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def completed_week_window(value: str, earliest: date | None = None, *,
                          now: datetime | None = None) -> tuple[str, str]:
    return usage_bounds(*completed_week_bounds(value, earliest, now=now))


def resolve_grain(kind: str, timezone_name: str, *, now: datetime | None = None, averaged: bool = False) -> Grain:
    """The page grain in the viewer's zone, or the HTTP error a bad grain or zone is."""
    try:
        return scope.resolve_grain(kind, timezone_name, now=now, averaged=averaged)
    except scope.ScopeError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
