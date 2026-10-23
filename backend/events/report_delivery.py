"""Shared delivery path for validated measurement reports."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from connections import binder
from events.connectors.measurement import MEASUREMENT_CONNECTOR
from events.platform import PlatformDelivery, deliver_platform_event_sync
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
