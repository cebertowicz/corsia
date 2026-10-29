"""Cost tracking API routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from fastapi import APIRouter, HTTPException

from connections import fact_memo
from infra.auth import authenticate
from infra.database import get_db
from infra.db_async import run_bulk, run_in_db
from infra.time import utc_now
from core.cost_identity import IDENTITY_GROUP, IdentityResolver, identity_columns, identity_join
from api.model_groups import group_spend
from api.measurement_scope import completed_week_window
from api.measurement_scope import scope_cte, validate_scope
from api.usage_rollup import rollup_bound, schedule_refresh, usage_source

router = APIRouter(prefix="/api/costs", tags=["costs"])


def _period_totals(db):
    """Read known spend for today and this month and missing prices without repricing history: this UTC month and day."""
    today = utc_now().date()
    month = today.replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    sql = (
        "SELECT COALESCE(SUM(u.cost_usd), 0) as cost_usd, "
        f"COALESCE(SUM(u.records) - SUM(u.cost_records), 0) as unknown_cost_rows FROM {usage_source(None)} u"
    )

    def spend(start: date, end: date):
        from_ts, to_ts = start.isoformat(), end.isoformat()
        bound = {"from_ts": from_ts, "to_ts": to_ts, "rollup_through": rollup_bound(db, from_ts, to_ts)}
        return db.execute(sql, bound).fetchone()

    return spend(month, next_month), spend(today, today + timedelta(days=1))


# A summary row's call counts, summed from ``api.usage_rollup`` hourly groups.
_COUNTS = (
    "COALESCE(SUM(u.records) - SUM(u.cost_records), 0) as unknown_cost_rows, "
    "COALESCE(SUM(u.records), 0) as api_calls "
)


def _stored_cost_class(row):
    """Classify retained dollars, never today's availability of model rates."""
    from llm.pricing import is_external_model, is_local_model

    if is_local_model(row.get("model")):
        return "local"
    if is_external_model(row.get("model")) and not row["cost_usd"]:
        # Measurement's rule: an External call stores NULL or a legacy placeholder zero, both unpriced, never free.
        return "no_pricing"
    return "priced" if row["cost_usd"] is not None else "no_pricing"


@dataclass(frozen=True)
class _UsageScope:
    """A validated Spending scope bound to its SQL parameters.

    Spending reuses the Measurement project/agent thread closure so both
    surfaces count the same work.
    """

    params: dict
    all_usage: bool

    @property
    def cte(self) -> str:
        """Provide the canonical thread closure for every scope, including all."""
        return scope_cte()

    @property
    def usage(self) -> str:
        """The scope's hourly usage groups over the window (``api.usage_rollup``); All has no thread filter."""
        return usage_source(None if self.all_usage else "scope_threads")


def _usage_scope(db, user_id: str, project: str, agent: str | None) -> tuple[dict, _UsageScope]:
    """Validate a Measurement-compatible scope and bind its query parameters."""
    selected = validate_scope(db, user_id, project, agent)
    response = selected.response()
    return response, _UsageScope(
        selected.params(user_id),
        all_usage=selected.is_workspace,
    )


def _usage_window(value: str, days: int | None = None) -> tuple[str, str]:
    """Adapt canonical completed weeks or a provider day window to usage bounds.

    The completed-week path takes the one boundary pair Measurement uses, so the
    two surfaces cannot drift apart at an edge. The provider-day path keeps its
    documented window - the day ``days`` ago through now - with only its start
    edge written as a bare date, which is what "start of that day" means once
    usage rows keep mixed timestamp spellings.

    ``days`` is caller-supplied and unbounded, and this arithmetic is Python's, not
    SQLite's: a window the proleptic Gregorian calendar cannot hold raises
    OverflowError inside the query worker, where the app's catch-all handler would
    report it as a 500 for what is really an unusable request. So the window is asked
    of the calendar and refused with 400 naming the permitted interval, derived from
    ``date.min`` and ``date.max`` against this same clock rather than capped by a
    number invented here. Both signs are bounded, because a negative ``days`` lands
    its start in the future - and answers empty, as it always did - until it too
    passes ``date.max``. The completed-week path's ``date.min`` is not at risk: it is
    a Monday by definition, so Measurement's floor subtracts zero days from it.
    """
    now = utc_now()
    if days is None:
        return completed_week_window(value, date.min, now=now)
    try:
        start = now.date() - timedelta(days=days)
    except OverflowError as error:
        low, high = -(date.max - now.date()).days, (now.date() - date.min).days
        raise HTTPException(
            400,
            f"days must be between {low} and {high}: that window is not a date the "
            "proleptic Gregorian calendar can hold",
        ) from error
    return start.isoformat(), now.isoformat()


def _weekly_costs(db, scope: _UsageScope):
    """Weekly token and cost totals over completed Monday bounds."""
    return db.execute(
        scope.cte + "SELECT date(substr(u.hour,1,10),'weekday 0','-6 days') as week, "
        "SUM(u.prompt_tokens) as prompt_tokens, "
        "SUM(u.completion_tokens) as completion_tokens, "
        "SUM(u.total_tokens) as total_tokens, "
        "SUM(u.cost_usd) as cost_usd, "
        + _COUNTS + f"FROM {scope.usage} u "
        "GROUP BY week ORDER BY week ASC",
        scope.params,
    ).fetchall()


@fact_memo.fact_reads()
def _costs_by_model(db, scope: _UsageScope):
    """Stored token and cost totals per model identity, most expensive first.

    Literal selectors resolve through ``core.cost_identity``, the identity Models
    uses. A merged identity keeps SQL's SUM semantics: its cost is the known sum,
    None only when none of its calls carried a cost, and its unpriced calls stay
    counted.
    """
    raw = db.execute(
        scope.cte + f"SELECT {identity_columns('u')},"
        "SUM(u.prompt_tokens) AS prompt_tokens,SUM(u.completion_tokens) AS completion_tokens,"
        "SUM(u.total_tokens) AS total_tokens,SUM(u.cost_usd) AS cost_usd,"
        + _COUNTS + f"FROM {scope.usage} u {identity_join('u')} GROUP BY {IDENTITY_GROUP}",
        scope.params,
    ).fetchall()
    resolve = IdentityResolver(db)
    grouped: dict[str, dict] = {}
    for row in raw:
        item = dict(row)
        identity = resolve(item)
        target = grouped.setdefault(identity.key, {
            "model": identity.key, "label": identity.label, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cost_usd": None, "unknown_cost_rows": 0, "api_calls": 0,
        })
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "unknown_cost_rows", "api_calls"):
            target[key] += int(item[key] or 0)
        if item["cost_usd"] is not None:
            target["cost_usd"] = (target["cost_usd"] or 0.0) + float(item["cost_usd"])
    return sorted(grouped.values(), key=lambda item: (item["cost_usd"] is None, -(item["cost_usd"] or 0), item["model"]))


def _costs_by_project(db, scope: _UsageScope):
    """Token and cost totals per project, unassigned threads grouped together."""
    return db.execute(
        scope.cte + "SELECT COALESCE(s.project_id, '__none__') as project_id, "
        "COALESCE(p.name, 'No project') as project_name, "
        "SUM(u.total_tokens) as total_tokens, "
        "SUM(u.cost_usd) as cost_usd, "
        + _COUNTS + f"FROM {scope.usage} u "
        "LEFT JOIN threads s ON u.thread_id = s.id "
        "LEFT JOIN projects p ON s.project_id = p.id "
        "GROUP BY COALESCE(s.project_id, '__none__') ORDER BY cost_usd DESC",
        scope.params,
    ).fetchall()


def _costs_by_source(db, scope: _UsageScope):
    """Token and cost totals per call source, most expensive first."""
    return db.execute(
        scope.cte + "SELECT u.source as source, "
        "SUM(u.total_tokens) as total_tokens, "
        "SUM(u.cost_usd) as cost_usd, "
        + _COUNTS + f"FROM {scope.usage} u "
        "GROUP BY u.source ORDER BY cost_usd DESC",
        scope.params,
    ).fetchall()


def _costs_by_thread(db, scope: _UsageScope):
    """Top ten threads by cost over the window."""
    return db.execute(
        scope.cte + "SELECT u.thread_id, s.title, s.project_id, "
        "SUM(u.total_tokens) as total_tokens, "
        "SUM(u.cost_usd) as cost_usd, "
        + _COUNTS + f"FROM {scope.usage} u "
        "LEFT JOIN threads s ON u.thread_id = s.id "
        "GROUP BY u.thread_id ORDER BY cost_usd DESC, u.thread_id LIMIT 10",
        scope.params,
    ).fetchall()


def _cost_totals(db, scope: _UsageScope):
    """Window-wide token and cost totals."""
    return db.execute(
        scope.cte + "SELECT "
        "SUM(u.prompt_tokens) as prompt_tokens, "
        "SUM(u.completion_tokens) as completion_tokens, "
        "SUM(u.total_tokens) as total_tokens, "
        "SUM(u.cost_usd) as cost_usd, "
        + _COUNTS + f"FROM {scope.usage} u",
        scope.params,
    ).fetchone()


@router.get("/summary")
async def cost_summary(token: str, project: str = "all", agent: str = "none", range: str = "12w", days: int | None = None):
    """Get scoped Spending aggregates for Measurement or provider day windows."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _query():
        db = get_db()
        selected_scope, usage_scope = _usage_scope(db, user["id"], project, agent)
        from_ts, to_ts = _usage_window(range, days)
        usage_scope.params.update(from_ts=from_ts, to_ts=to_ts, rollup_through=rollup_bound(db, from_ts, to_ts))
        weekly = _weekly_costs(db, usage_scope)
        by_model = _costs_by_model(db, usage_scope)
        by_project = _costs_by_project(db, usage_scope)
        by_source = _costs_by_source(db, usage_scope)
        by_thread = _costs_by_thread(db, usage_scope)
        totals = _cost_totals(db, usage_scope)
        # Today and this month are workspace-wide, so they stay unscoped.
        monthly, today_row = _period_totals(db)
        db.close()
        return {
            "scope": selected_scope,
            "period_days": days,
            "range": None if days is not None else range,
            "weekly": [dict(r) for r in weekly],
            "by_model": [dict(r) for r in by_model],
            "by_project": [dict(r) for r in by_project],
            "by_source": [dict(r) for r in by_source],
            "by_thread": [dict(r) for r in by_thread],
            "totals": dict(totals) if totals else {},
            "monthly": monthly,
            "today": today_row,
        }

    data = await run_bulk(_query)
    schedule_refresh()
    weekly = data["weekly"]
    monthly = data["monthly"]
    today = data["today"]

    by_model_tagged = [{**row, "cost_class": _stored_cost_class(row)} for row in data["by_model"]]

    today_cost = today["cost_usd"] if today else 0
    monthly_cost = monthly["cost_usd"] if monthly else 0

    return {
        "period_days": data["period_days"],
        "scope": data["scope"],
        "range": data["range"],
        "totals": data["totals"],
        "weekly": weekly,
        "by_model": by_model_tagged,
        # Spend's rows as shown (api.model_groups); by_model stays whole for readers that filter it.
        "by_model_shown": group_spend(by_model_tagged),
        "by_source": data["by_source"],
        "by_project": data["by_project"],
        "top_threads": data["by_thread"],
        "monthly_cost_usd": round(monthly_cost, 4),
        "period_spend": {
            "today_cost_usd": round(today_cost, 4),
            "monthly_cost_usd": round(monthly_cost, 4),
            "today_unknown_cost_rows": today["unknown_cost_rows"],
            "monthly_unknown_cost_rows": monthly["unknown_cost_rows"],
        },
    }
