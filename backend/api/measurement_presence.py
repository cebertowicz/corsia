"""Canonical presence interval-union SQL builders for Measurement attention."""

from __future__ import annotations

from core.measurement_scope import Grain


def presence_predicate(scope_threads: str, *, alias: str = "v") -> str:
    """Return the presence filter one composite scope owns.

    A project-level visit has no thread. It belongs to All work and to its project,
    never to an agent, whose answer is exclusively its thread closure.
    """
    closure = f"{alias}.thread_id IN (SELECT id FROM {scope_threads})"
    project_only = f"{alias}.thread_id IS NULL"
    return (
        f"({closure} OR (:scope_agent IS NULL AND "
        f"((:scope_project='all' AND {project_only}) OR "
        f"(:scope_project<>'all' AND {alias}.project_id=:scope_project))))"
    )


def presence_allocation_cte(grain: Grain) -> str:
    """Return one interval-union timeline and equal threaded allocations.

    Every interval is clipped to the request range. Its starts, ends, and local
    period boundaries partition time into slices on which the set of visits is
    unchanged. ``presence_live_segments`` counts each slice once; ``presence_thread_slices``
    divides a threaded slice evenly across the active threads. The latter is the only
    source for agent attribution, so agent rows add to the threaded All-work total.
    """
    boundaries = " UNION ALL ".join(
        [*(f"SELECT :gb{index} AS instant,0 AS delta" for index in range(len(grain.starts))),
         "SELECT :gend_partial AS instant,0 AS delta"]
    )
    return f"""
      presence_intervals AS (
        SELECT v.thread_id,v.project_id,
          MAX(datetime(v.entered_at),datetime(:from_ts)) AS began,
          MIN(datetime(v.exited_at),datetime(:to_ts)) AS ended
        FROM presence_visits v LEFT JOIN threads t ON t.id=v.thread_id
        WHERE v.entered_at<:to_ts AND v.exited_at>:from_ts
          AND (
            (v.thread_id IS NOT NULL AND t.user_id=:user_id)
            OR (v.thread_id IS NULL AND v.project_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM threads project_thread
              WHERE project_thread.project_id=v.project_id AND project_thread.user_id=:user_id
            ))
          )
        UNION ALL
        SELECT p.thread_id,t.project_id,
          MAX(datetime(p.last_entered_at),datetime(:from_ts)) AS began,
          MIN(datetime('now'),datetime(:to_ts)) AS ended
        FROM thread_presence p JOIN threads t ON t.id=p.thread_id
        WHERE t.user_id=:user_id AND p.present=1
          AND p.last_entered_at<:to_ts AND datetime('now')>:from_ts
      ), presence_events AS (
        SELECT began AS instant,1 AS delta FROM presence_intervals WHERE began<ended
        UNION ALL SELECT ended AS instant,-1 AS delta FROM presence_intervals WHERE began<ended
        UNION ALL {boundaries}
      ), presence_event_totals AS (
        SELECT instant,SUM(delta) AS delta FROM presence_events GROUP BY instant
      ), presence_coverage AS (
        SELECT instant,SUM(delta) OVER (ORDER BY instant) AS active_visits FROM presence_event_totals
      ), presence_segments AS (
        SELECT instant AS began,LEAD(instant) OVER (ORDER BY instant) AS ended,active_visits
        FROM presence_coverage
      ), presence_live_segments AS (
        SELECT began,ended,(julianday(ended)-julianday(began))*86400.0 AS seconds
        FROM presence_segments WHERE active_visits>0 AND ended IS NOT NULL AND began<ended
      ), presence_active_threads AS (
        SELECT s.began,s.ended,v.thread_id
        FROM presence_live_segments s JOIN presence_intervals v ON v.began<=s.began AND v.ended>=s.ended
        WHERE v.thread_id IS NOT NULL GROUP BY s.began,s.ended,v.thread_id
      ), presence_thread_slices AS (
        SELECT a.thread_id,a.began,a.ended,
          (julianday(a.ended)-julianday(a.began))*86400.0 /
          COUNT(*) OVER (PARTITION BY a.began,a.ended) AS seconds
        FROM presence_active_threads a
      )"""


def active_family_sql(grain: Grain, scope_threads: str, outcomes: str) -> str:
    """Return each scope's union-presence time and outcome count by period."""
    predicate = presence_predicate(scope_threads)
    return f"""
      WITH {presence_allocation_cte(grain)}, scoped_attention AS (
        SELECT s.began,s.ended,s.seconds FROM presence_live_segments s
        WHERE :scope_agent IS NULL AND EXISTS (
          SELECT 1 FROM presence_intervals v WHERE v.began<=s.began AND v.ended>=s.ended AND {predicate}
        )
        UNION ALL
        SELECT s.began,s.ended,s.seconds FROM presence_thread_slices s
        WHERE :scope_agent IS NOT NULL AND s.thread_id IN (SELECT id FROM {scope_threads})
      ), active_periods AS (
        SELECT {grain.bucket('began')} AS period,SUM(seconds)/3600.0 AS active_hours
        FROM scoped_attention WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ) SELECT a.period,a.active_hours,COALESCE(o.outcomes,0) AS outcomes FROM active_periods a
        LEFT JOIN {outcomes} o USING(period) ORDER BY a.period
    """
