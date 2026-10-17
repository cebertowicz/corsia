"""Canonical presence interval-union SQL builders for Measurement attention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    def boundary(name: str) -> str:
        # Grain boundaries deliberately omit seconds; normalize them before they join
        # interval instants so SQLite can order and subtract one timestamp format.
        return f"datetime({name} || CASE length({name}) WHEN 13 THEN ':00' ELSE '' END)"

    boundaries = " UNION ALL ".join(
        [*(f"SELECT {boundary(f':gb{index}')} AS instant,0 AS delta" for index in range(len(grain.starts))),
         f"SELECT {boundary(':gend_partial')} AS instant,0 AS delta"]
    )
    return f"""
      presence_intervals AS MATERIALIZED (
        SELECT a.thread_id,t.project_id,
          MAX(datetime(a.focused_at),datetime(:from_ts)) AS began,
          MIN(COALESCE(datetime(a.blurred_at),datetime('now')),datetime(:to_ts)) AS ended
        FROM measurement_focus_intervals a JOIN threads t ON t.id=a.thread_id
        WHERE t.user_id=:user_id AND a.focused_at<:to_ts
          AND COALESCE(datetime(a.blurred_at),datetime('now'))>:from_ts
        UNION ALL
        SELECT v.thread_id,v.project_id,
          MAX(datetime(v.entered_at),datetime(:from_ts)) AS began,
          MIN(datetime(v.exited_at),datetime(COALESCE(f.focused_since, v.exited_at)),datetime(:to_ts)) AS ended
        FROM threads t JOIN presence_visits v ON v.thread_id=t.id
        LEFT JOIN measurement_focus_users f ON f.user_id=v.user_id
        WHERE t.user_id=:user_id AND v.entered_at<:to_ts AND v.exited_at>:from_ts
          AND (f.focused_since IS NULL OR v.entered_at<f.focused_since)
        UNION ALL
        SELECT v.thread_id,v.project_id,
          MAX(datetime(v.entered_at),datetime(:from_ts)) AS began,
          MIN(datetime(v.exited_at),datetime(COALESCE(f.focused_since, v.exited_at)),datetime(:to_ts)) AS ended
        FROM presence_visits v LEFT JOIN measurement_focus_users f ON f.user_id=v.user_id
        WHERE v.thread_id IS NULL AND v.project_id IS NOT NULL
          AND v.user_id=:user_id
          AND v.entered_at<:to_ts AND v.exited_at>:from_ts
          AND (f.focused_since IS NULL OR v.entered_at<f.focused_since)
        UNION ALL
        SELECT p.thread_id,t.project_id,
          MAX(datetime(p.last_entered_at),datetime(:from_ts)) AS began,
          MIN(COALESCE(datetime(p.exit_pending_at),datetime('now')),datetime(:to_ts)) AS ended
        FROM thread_presence p JOIN threads t ON t.id=p.thread_id
        LEFT JOIN measurement_focus_users f ON f.user_id=p.user_id
        WHERE t.user_id=:user_id AND p.present=1
          AND p.last_entered_at<:to_ts AND COALESCE(datetime(p.exit_pending_at),datetime('now'))>:from_ts
          AND f.user_id IS NULL
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
      ), presence_live_segments AS MATERIALIZED (
        SELECT began,ended,(julianday(ended)-julianday(began))*86400.0 AS seconds
        FROM presence_segments WHERE active_visits>0 AND ended IS NOT NULL AND began<ended
      ), presence_active_threads AS MATERIALIZED (
        SELECT s.began,s.ended,v.thread_id
        FROM presence_live_segments s JOIN presence_intervals v ON v.began<=s.began AND v.ended>=s.ended
        WHERE v.thread_id IS NOT NULL GROUP BY s.began,s.ended,v.thread_id
      ), presence_thread_slices AS (
        SELECT a.thread_id,a.began,a.ended,
          (julianday(a.ended)-julianday(a.began))*86400.0 /
          COUNT(*) OVER (PARTITION BY a.began,a.ended) AS seconds
        FROM presence_active_threads a
      )"""


@dataclass(frozen=True)
class PresenceCoverage:
    """The one owner of presence coverage: when it starts and what an empty period means.

    A presence-backed measure is measured from the viewer-local day (or that day's
    week) of the user's first recorded presence. Inside a measured period, no presence
    is a true zero -- the user was not there -- so a period with outcomes and no
    presence still counts, with zero time. A rate that divides by presence time (the
    size rate) starts at the later of its own instrument start and that day. The series,
    the Agents table and the index all read these answers from here.
    """

    started_at: str | None
    zone: str

    @property
    def start_day(self) -> str | None:
        """The viewer-local date of the first recorded presence."""
        if self.started_at is None:
            return None
        instant = datetime.fromisoformat(self.started_at).replace(tzinfo=timezone.utc)
        return instant.astimezone(ZoneInfo(self.zone)).date().isoformat()

    def start_period(self, grain: Grain) -> str | None:
        """The first local day, or the week holding it, a presence-backed measure covers."""
        day = self.start_day
        if day is None or grain.kind == "days":
            return day
        local = datetime.fromisoformat(day).date()
        return (local - timedelta(days=local.weekday())).isoformat()

    def rate_start(self, instrument_start: str) -> str:
        """The first day a rate over presence time is measured, given its own instrument start."""
        day = self.start_day
        return max(instrument_start, day) if day else instrument_start


def presence_coverage(db, user_id: str, zone: str) -> PresenceCoverage:
    """Read the user's first recorded presence across retained sources.

    Ownership follows ``presence_intervals``: a threaded visit belongs to its thread's
    owner, a project-level visit to its recorded user.
    """
    row = db.execute("""
        SELECT MIN(instant) AS instant FROM (
          SELECT datetime(v.entered_at) AS instant
          FROM presence_visits v JOIN threads t ON t.id=v.thread_id
          LEFT JOIN measurement_focus_users f ON f.user_id=v.user_id
          WHERE t.user_id=:user_id AND (f.focused_since IS NULL OR v.entered_at<f.focused_since)
          UNION ALL
          SELECT datetime(v.entered_at) AS instant FROM presence_visits v
          LEFT JOIN measurement_focus_users f ON f.user_id=v.user_id
          WHERE v.thread_id IS NULL AND v.user_id=:user_id
            AND (f.focused_since IS NULL OR v.entered_at<f.focused_since)
          UNION ALL
          SELECT datetime(a.focused_at) AS instant
          FROM measurement_focus_intervals a JOIN threads t ON t.id=a.thread_id
          WHERE t.user_id=:user_id
          UNION ALL
          SELECT datetime(p.last_entered_at) AS instant
          FROM thread_presence p JOIN threads t ON t.id=p.thread_id
          WHERE t.user_id=:user_id AND NOT EXISTS (
            SELECT 1 FROM measurement_focus_users f WHERE f.user_id=p.user_id)
        ) WHERE instant IS NOT NULL
    """, {"user_id": user_id}).fetchone()
    return PresenceCoverage(row["instant"] if row and row["instant"] else None, zone)


def first_presence_period(db, user_id: str, grain: Grain) -> str | None:
    """Return the first local day or week a presence-backed measure covers."""
    return presence_coverage(db, user_id, grain.zone).start_period(grain)


def scoped_presence_cte(grain: Grain, scope_threads: str) -> str:
    """Return the one scoped presence time every presence-backed series reads.

    ``scoped_attention`` is the scope's union time: a composite scope counts each slice
    once, an agent its evenly split thread slices. ``scoped_visits`` are the scope's
    clipped intervals, the observations behind that time: a legacy visit before a user's
    cutover, a focus interval (one per blur and refocus) from it on.
    """
    predicate = presence_predicate(scope_threads)
    return f"""{presence_allocation_cte(grain)}, scoped_attention AS (
        SELECT s.began,s.ended,s.seconds FROM presence_live_segments s
        WHERE :scope_agent IS NULL AND EXISTS (
          SELECT 1 FROM presence_intervals v WHERE v.began<=s.began AND v.ended>=s.ended AND {predicate}
        )
        UNION ALL
        SELECT s.began,s.ended,s.seconds FROM presence_thread_slices s
        WHERE :scope_agent IS NOT NULL AND s.thread_id IN (SELECT id FROM {scope_threads})
      ), scoped_visits AS (
        SELECT v.began FROM presence_intervals v WHERE v.began<v.ended AND {predicate}
      )"""


def presence_family_sql(grain: Grain, scope_threads: str) -> str:
    """Return each scope's presence hours by period, with n the intervals that began in it.

    After a user's cutover each focus interval counts, so n is not a count of sittings.
    """
    return f"""
      WITH {scoped_presence_cte(grain, scope_threads)}, hours AS (
        SELECT {grain.bucket('began')} AS period,SUM(seconds)/3600.0 AS value
        FROM scoped_attention WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ), visits AS (
        SELECT {grain.bucket('began')} AS period,COUNT(*) AS n
        FROM scoped_visits WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ) SELECT h.period,h.value,COALESCE(c.n,0) AS n FROM hours h LEFT JOIN visits c USING(period) ORDER BY h.period
    """


def active_family_sql(grain: Grain, scope_threads: str, outcomes: str) -> str:
    """Return each scope's union-presence time and outcome count by period.

    A period with outcomes and no presence is served with zero time (``PresenceCoverage``);
    the caller withholds periods before coverage starts.
    """
    return f"""
      WITH {scoped_presence_cte(grain, scope_threads)}, active_periods AS (
        SELECT {grain.bucket('began')} AS period,SUM(seconds)/3600.0 AS active_hours
        FROM scoped_attention WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ), active_spine AS (
        SELECT period FROM active_periods UNION SELECT period FROM {outcomes}
      ) SELECT s.period,COALESCE(a.active_hours,0) AS active_hours,COALESCE(o.outcomes,0) AS outcomes
        FROM active_spine s LEFT JOIN active_periods a USING(period) LEFT JOIN {outcomes} o USING(period)
        ORDER BY s.period
    """
