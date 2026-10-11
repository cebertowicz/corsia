"""Measurement key figures: the current period beside the previous one, from the same scope.

A period is a half-open [start, end) pair of stored UTC bounds, so the same builder serves
"today vs yesterday" now and any other pair of consecutive periods later. Token figures use
the one token definition in ``measurement_queries``. A period with no calls recorded nothing
to miss, so its tokens are a known zero; a figure is a lower bound, never hidden, where some
calls did not record it.
"""
from __future__ import annotations

from typing import Any, NamedTuple

# Token completeness derives from persisted usage below; there is no install-global date.
from api.measurement_token_sql import TOKEN_FIGURE_SQL


_TOKEN_COMPLETE = """
    u.total_tokens IS NOT NULL OR (
      u.prompt_tokens IS NOT NULL AND u.completion_tokens IS NOT NULL
      AND u.cache_creation_tokens IS NOT NULL AND u.cache_read_tokens IS NOT NULL
    )
"""
_TOKEN_RECORDED_FROM: dict[Any, str] = {}


def _tokens_recorded_from(db) -> str | None:
    """First day whose persisted usage can supply the complete token figure.

    Token-recording completeness is installation history, not a release date:
    an upgraded installation may begin later, while a fresh one may begin earlier.
    """
    cached = _TOKEN_RECORDED_FROM.get(db)
    if cached is not None:
        return cached
    row = db.execute(
        f"""SELECT substr(u.created_at, 1, 10) AS day FROM llm_usage u
            WHERE {_TOKEN_COMPLETE}
            ORDER BY u.created_at ASC LIMIT 1"""
    ).fetchone()
    recorded_from = str(row["day"]) if row and row["day"] is not None else None
    if recorded_from is not None:
        _TOKEN_RECORDED_FROM[db] = recorded_from
    return recorded_from


class Period(NamedTuple):
    start: str
    end: str


_FIGURES = ", ".join(
    f"SUM({value}) AS {name}, SUM({unknown}) AS {name}_unknown" for name, value, unknown in TOKEN_FIGURE_SQL
)
_TOKEN_SQL = f"""
    SELECT CASE WHEN u.created_at >= :current_from THEN 'current' ELSE 'previous' END AS period, {_FIGURES}
    FROM llm_usage u
    WHERE u.thread_id IN (SELECT id FROM measurement_scope_threads)
      AND u.created_at >= :previous_from AND u.created_at < :current_to
    GROUP BY period
"""



def _token_figure(row: dict[str, Any] | None, name: str) -> dict[str, int]:
    """One token figure: the sum of what was recorded, and how many calls recorded none of it."""
    if row is None:
        return {"value": 0, "unknown_records": 0}
    return {"value": int(row[name] or 0), "unknown_records": int(row[f"{name}_unknown"] or 0)}


def build_kpi(db, current: Period, previous: Period) -> dict[str, Any]:
    """Build both periods' token figures on a connection whose scope table is already prepared. One read."""
    bounds = {"previous_from": previous.start, "current_from": current.start, "current_to": current.end}
    tokens = {row["period"]: dict(row) for row in db.execute(_TOKEN_SQL, bounds).fetchall()}
    recorded_from = _tokens_recorded_from(db)
    return {
        name: {
            "start": period.start, "end": period.end,
            "tokens_recorded": recorded_from is not None and period.start >= recorded_from,
            "tokens": {key: _token_figure(tokens.get(name), key) for key, _, _ in TOKEN_FIGURE_SQL},
        }
        for name, period in (("current", current), ("previous", previous))
    }
