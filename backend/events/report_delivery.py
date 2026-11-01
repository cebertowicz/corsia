"""Shared delivery path for validated measurement reports."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from connections import binder
from events.connectors.measurement import MEASUREMENT_CONNECTOR
from events.connectors import get_manifest
from events.envelope import normalize
from events.platform import PlatformDelivery, deliver_envelope_sync, deliver_platform_event_sync
from events.resolve import resolve_direct
from infra.database import write_transaction


def deliver_measurement_report_sync(
    *,
    scope: str,
    event_type: str,
    payload: Mapping[str, Any],
    transaction_scope: str,
    db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Bind a scope at delivery time and deliver its validated report."""
    with write_transaction(db, scope=transaction_scope) as conn:
        binder.ensure_designation_rule(
            connector=MEASUREMENT_CONNECTOR, fact="scope", value=scope,
            designation=scope, db=conn)
        return deliver_platform_event_sync(
            MEASUREMENT_CONNECTOR, dict(payload), event_type, db=conn)


def deliver_direct_report_sync(
    *,
    thread_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    transaction_scope: str,
    db: sqlite3.Connection | None = None,
) -> list[PlatformDelivery]:
    """Deliver a validated report straight to one named thread, with no designation rule."""
    envelope = normalize(MEASUREMENT_CONNECTOR, dict(payload), event_type)
    with write_transaction(db, scope=transaction_scope) as conn:
        deliveries, _ = deliver_envelope_sync(
            envelope, get_manifest(MEASUREMENT_CONNECTOR).decl(envelope.type),
            lambda c: [resolve_direct(thread_id, envelope.event_class, db=c)], db=conn)
        return deliveries
