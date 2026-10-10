"""Actual-cost completeness for Measurement's Cost section.

Actual spend is known only where the ledger can say what was charged. The
subscription policy is ``llm.pricing``'s, applied to the recorded model and
source: subscription usage has no per-call charge, so it is never a direct
charge, even when a legacy row carries a ``cost_usd``; the workspace
subscription bill is reported beside it, not allocated into it, and only for
All work. Everything is read from the scoped ledger rows, never from how
many accounts are signed in or from the current model catalog.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from api.measurement_external_actual import external_actual
from api.measurement_prepared import PREPARED_SCOPE
from api.usage_rollup import usage_params, usage_source
from core.measurement_scope import Grain, is_workspace_scope
from llm.pricing import is_external_model, is_historical_subscription_usage, is_local_model


def build_actual(db, params: dict[str, Any]) -> dict[str, Any]:
    """Scoped direct charges, plus the workspace subscription bill at All work only.

    ``complete`` is the scope's own: every direct-charge record in it has a known
    charge. The workspace bill and other scopes' usage never change it.
    """
    grain: Grain = params["grain"]
    # Actual sits under the headline for the latest comparison window, so it sums exactly that
    # window's local periods: never the older periods and never the one still in progress.
    first, last = grain.latest.first, grain.period_end(grain.latest.last)
    start, end = grain.instants(grain.latest)
    window = {"from_ts": start[:10], "to_ts": (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()}
    bounded = {**usage_params(db, {**params, **window}), "actual_window": grain.window_sql("u.created_at", grain.latest)}
    rows = db.execute(
        f"""
        SELECT u.model AS model, u.source AS source, SUM(u.records) AS records,
          SUM(u.cost_records) AS charged_records, SUM(COALESCE(u.cost_usd,0)) AS dollars
        FROM {usage_source(PREPARED_SCOPE, bounded['grain'])} u
        WHERE {grain.window_sql("u.hour", grain.latest)}
        GROUP BY u.model, u.source
        """,
        bounded,
    ).fetchall()
    subscription_records = unknown_charge_records = 0
    direct_dollars = 0.0
    for row in rows:
        model = row["model"]
        if is_local_model(model) or is_external_model(model):
            continue  # oMLX is never billed; External endpoints are classified from their billing below
        if is_historical_subscription_usage(model, row["source"]):
            subscription_records += int(row["records"])
        else:
            direct_dollars += float(row["dollars"])
            unknown_charge_records += int(row["records"]) - int(row["charged_records"])
    external = external_actual(db, bounded)
    subscription_records += external.subscription_records
    unknown_charge_records += external.unknown_charge_records
    direct_dollars += external.direct_dollars
    from connections.subscription_billing import workspace_context

    # The bill belongs to the workspace, so no project or agent view carries it.
    workspace_subscription = (
        workspace_context(db, from_ts=first, to_ts=(date.fromisoformat(last) + timedelta(days=1)).isoformat())
        if is_workspace_scope(bounded["scope_project"], bounded["scope_agent"]) else None
    )
    return {
        "subscription_records": subscription_records,
        "unknown_charge_records": unknown_charge_records,
        "direct_api_dollars": round(direct_dollars, 10),
        "complete": unknown_charge_records == 0,
        # The part of the unknown charges fixed by setting an External endpoint's billing.
        "external_unset_records": external.unset_records,
        # A declared workspace bill is intentionally separate from scoped calls:
        # facts cover zero-call dates and removed account references, never allocation.
        "workspace_subscription": workspace_subscription,
    }
