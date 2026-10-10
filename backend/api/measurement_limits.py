"""Read and classify Measurement usage-limit turns without duplicating provider rules."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from zoneinfo import ZoneInfo

from api.measurement_scope import resolve_grain, validate_scope
from core.measurement_scope import Grain, query_params, scope_cte
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_bulk
from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

router = APIRouter()


NEXT_KINDS = ("retried", "moved_model", "moved_account", "no_next_turn", "unknown_next")


def _utc_timestamp(value: str) -> datetime:
    """Read stored naive timestamps as UTC, never the host's local time."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _period(grain: Grain, value: str) -> str:
    """The grain's period a stored UTC timestamp falls in, by the viewer's local date."""
    return grain.period_of(_utc_timestamp(value).astimezone(ZoneInfo(grain.zone)).date().isoformat())


def _seconds(started: str | None, ended: str | None) -> int | None:
    if not started or not ended:
        return None
    return max(0, int((_utc_timestamp(started) - _utc_timestamp(ended)).total_seconds()))


def fetch_limit_hits(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return classified scope-local usage-limit errors and their next turn, in the grain's range.

    SQL selects only persisted turn facts and the scope closure. The provider's
    canonical classifier deliberately runs here in Python, on the exact stored
    error text, so every Measurement consumer shares the same definition.
    """
    bounded = query_params(params)
    grain: Grain = bounded["grain"]
    rows = db.execute(scope_cte() + """
        , ordered_turns AS (
          SELECT turns.*, LEAD(started_at) OVER thread_order AS next_started_at,
                 LEAD(model) OVER thread_order AS next_model,
                 LEAD(vendor_account_id) OVER thread_order AS next_account_id
          FROM active_turns turns
          WHERE turns.thread_id IN (SELECT id FROM scope_threads)
          WINDOW thread_order AS (PARTITION BY thread_id ORDER BY turn_order)
        )
        SELECT * FROM ordered_turns
        WHERE status='error' AND ended_at >= :from_ts AND ended_at < :to_ts
        ORDER BY ended_at, turn_order
    """, bounded).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if not is_provider_usage_or_quota_limit(item["error"]):
            continue
        account, next_account = item["vendor_account_id"], item["next_account_id"]
        model, next_model = item["model"], item["next_model"]
        if item["next_started_at"] is None:
            kind = "no_next_turn"
        elif account and next_account and account != next_account:
            kind = "moved_account"
        elif account and next_account and account == next_account and model and next_model:
            kind = "moved_model" if model != next_model else "retried"
        else:
            # Missing identity never proves either a retry or a move.
            kind = "unknown_next"
        period = _period(grain, item["ended_at"])
        if period in grain.periods:
            result.append({"period": period, "next": kind,
                           "wait_seconds": _seconds(item["next_started_at"], item["ended_at"])})
    return result


def limits_by_period(db, params: dict[str, Any]) -> dict[str, int]:
    return dict(Counter(item["period"] for item in fetch_limit_hits(db, params)))


def first_recorded_period(db, params: dict[str, Any]) -> str | None:
    """Return the first retained in-scope turn's period, independent of limit errors.

    A zero in a period is meaningful only after this scope had a retained turn to
    observe. The caller still clamps this evidence to the metric instrument and
    requested range.
    """
    row = db.execute(scope_cte() + """
        SELECT MIN(started_at) AS started_at FROM active_turns
        WHERE thread_id IN (SELECT id FROM scope_threads)
    """, query_params(params)).fetchone()
    return _period(params["grain"], row["started_at"]) if row and row["started_at"] else None


def account_usage_summary(db) -> dict[str, int | None] | None:
    """Project the current account facts through their existing canonical owner."""
    from connections.harness_usage import account_usage, stored_usage
    from connections.vendor_accounts import list_accounts

    accounts = list_accounts(db=db)
    if not accounts:
        return None
    fully_used = overage = credits = 0
    has_credits_fact = False
    for account in accounts:
        usage = account_usage(stored_usage(provider=account["provider"], account_id=account["id"], db=db),
                              provider=account["provider"])
        fully_used += int(any(window.get("state") == "reported" and window.get("used_percent") == 100
                              for window in usage["windows"] if isinstance(window, dict)))
        overage += int(usage["allow_overage"])
        fact = usage["facts"].get("credits")
        if isinstance(fact, dict):
            has_credits_fact = True
            credits += int(fact.get("hasCredits") is True or fact.get("unlimited") is True)
    return {"total": len(accounts), "fully_used": fully_used, "overage": overage,
            "credits": credits if has_credits_fact else None}


def limit_summary(hits: list[dict[str, Any]]) -> dict[str, Any]:
    breakdown = Counter(item["next"] for item in hits)
    waits = sorted(item["wait_seconds"] for item in hits if item["wait_seconds"] is not None)
    # Nearest-rank p90 makes the stated "1 in 10 waited X or more" a mechanical
    # rule, including short histories where interpolation would invent precision.
    p90 = waits[max(0, (9 * len(waits) + 9) // 10 - 1)] if waits else None
    median = waits[(len(waits) - 1) // 2] if waits else None
    return {"hits": len(hits), "next": {kind: breakdown[kind] for kind in NEXT_KINDS},
            "median_wait_seconds": median, "p90_wait_seconds": p90}


@router.get("/limits")
async def measurement_limits(
    project: str = Query(...), agent: str = Query(...), grain: str = "days",
    timezone_name: str = Query(..., alias="timezone"), user: dict = Depends(require_user),
):
    """Return the latest comparison window's classified limit-hit detail beside the one before."""
    page_grain = resolve_grain(grain, timezone_name)

    def query() -> tuple[dict[str, str | None], dict[str, Any], int, dict[str, int | None] | None]:
        db = get_db()
        try:
            selected_scope = validate_scope(db, user["id"], project, agent)
            hits = fetch_limit_hits(db, {**selected_scope.params(user["id"]), "grain": page_grain})
            latest = limit_summary([hit for hit in hits if hit["period"] in page_grain.latest.periods])
            previous = limit_summary([hit for hit in hits if hit["period"] in page_grain.previous.periods])
            # Accounts are workspace-wide, so only All work carries them.
            accounts = account_usage_summary(db) if selected_scope.is_workspace else None
            return selected_scope.response(), latest, int(previous["hits"]), accounts
        finally:
            db.close()

    scope, latest, previous_hits, accounts = await run_bulk(query)
    return {"definition_version": 1, "scope": scope, "period": page_grain.describe(),
            "previous_hits": previous_hits, "accounts": accounts, **latest}
