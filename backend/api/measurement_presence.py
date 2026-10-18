"""Canonical presence interval-union SQL builders for Measurement attention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from api.measurement_presence_rollup import rolled_attention_cte
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


def _visit_intervals(since: str) -> str:
    """One row per visit overlapping ``[since, :to_ts)``, finished or live, clipped to it."""
    return f"""
        SELECT v.thread_id,v.project_id,
          MAX(datetime(v.entered_at),datetime({since})) AS began,
          MIN(datetime(v.exited_at),datetime(:to_ts)) AS ended,
          v.focus_spans,NULL AS focus_since
        FROM threads t JOIN presence_visits v ON v.thread_id=t.id
        WHERE t.user_id=:user_id AND v.entered_at<:to_ts AND v.exited_at>{since}
        UNION ALL
        SELECT v.thread_id,v.project_id,
          MAX(datetime(v.entered_at),datetime({since})) AS began,
          MIN(datetime(v.exited_at),datetime(:to_ts)) AS ended,
          v.focus_spans,NULL AS focus_since
        FROM presence_visits v
        WHERE v.thread_id IS NULL AND v.project_id IS NOT NULL
          AND v.user_id=:user_id
          AND v.entered_at<:to_ts AND v.exited_at>{since}
        UNION ALL
        SELECT p.thread_id,t.project_id,
          MAX(datetime(p.last_entered_at),datetime({since})) AS began,
          MIN(COALESCE(datetime(p.exit_pending_at),datetime('now')),datetime(:to_ts)) AS ended,
          p.focus_spans,p.focus_since
        FROM thread_presence p JOIN threads t ON t.id=p.thread_id
        WHERE t.user_id=:user_id AND p.present=1
          AND p.last_entered_at<:to_ts AND COALESCE(datetime(p.exit_pending_at),datetime('now'))>{since}"""


def presence_allocation_cte(grain: Grain, *, rolled: bool = False) -> str:
    """Return one interval-union timeline and equal threaded allocations.

    Every interval is clipped to the request range. ``presence_intervals`` holds one
    row per visit; ``presence_focus_spans`` expands each into the spans it was focused
    (NULL ``focus_spans`` predates focus tracking and counts whole; a live visit adds its
    open span). Span ends and local period boundaries partition time into slices on which
    the set of focused spans is unchanged. ``presence_live_segments`` counts each slice
    once; ``presence_thread_slices`` divides a threaded slice evenly across the active
    threads, the only source for agent attribution, so agent rows add to the All-work total.

    ``rolled`` reads the spans only from ``:focus_rollup_through`` on (``presence_tail_intervals``):
    the time before it is in the rollup (``api.presence_rollup``), which ``rolled_attention_cte``
    adds to the scope's time. Visits are still counted over the whole range.
    """
    def boundary(name: str) -> str:
        # Grain boundaries deliberately omit seconds; normalize them before they join
        # interval instants so SQLite can order and subtract one timestamp format.
        return f"datetime({name} || CASE length({name}) WHEN 13 THEN ':00' ELSE '' END)"

    boundaries = " UNION ALL ".join(
        [*(f"SELECT {boundary(f':gb{index}')} AS instant,0 AS delta" for index in range(len(grain.starts))),
         f"SELECT {boundary(':gend_partial')} AS instant,0 AS delta"]
    )
    spans_from = "presence_tail_intervals" if rolled else "presence_intervals"
    tail = f"presence_tail_intervals AS ({_visit_intervals(':focus_rollup_through')}\n      ), " if rolled else ""
    return f"""
      presence_intervals AS ({_visit_intervals(':from_ts')}
      ), {tail}presence_focus_spans AS (
        SELECT v.thread_id,v.project_id,v.began,v.ended FROM {spans_from} v
        WHERE v.focus_spans IS NULL
        UNION ALL
        SELECT v.thread_id,v.project_id,
          MAX(datetime(json_extract(j.value,'$[0]')),v.began) AS began,
          MIN(datetime(json_extract(j.value,'$[1]')),v.ended) AS ended
        FROM {spans_from} v,json_each(v.focus_spans) j WHERE v.focus_spans IS NOT NULL
        UNION ALL
        SELECT v.thread_id,v.project_id,MAX(datetime(v.focus_since),v.began) AS began,v.ended
        FROM {spans_from} v WHERE v.focus_spans IS NOT NULL AND v.focus_since IS NOT NULL
      ), presence_events AS (
        SELECT began AS instant,1 AS delta FROM presence_focus_spans WHERE began<ended
        UNION ALL SELECT ended AS instant,-1 AS delta FROM presence_focus_spans WHERE began<ended
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
        FROM presence_live_segments s JOIN presence_focus_spans v ON v.began<=s.began AND v.ended>=s.ended
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
          FROM presence_visits v JOIN threads t ON t.id=v.thread_id WHERE t.user_id=:user_id
          UNION ALL
          SELECT datetime(entered_at) AS instant FROM presence_visits
          WHERE thread_id IS NULL AND user_id=:user_id
          UNION ALL
          SELECT datetime(p.last_entered_at) AS instant
          FROM thread_presence p JOIN threads t ON t.id=p.thread_id
          WHERE t.user_id=:user_id
        ) WHERE instant IS NOT NULL
    """, {"user_id": user_id}).fetchone()
    return PresenceCoverage(row["instant"] if row and row["instant"] else None, zone)


def first_presence_period(db, user_id: str, grain: Grain) -> str | None:
    """Return the first local day or week a presence-backed measure covers."""
    return presence_coverage(db, user_id, grain.zone).start_period(grain)


def scoped_presence_cte(grain: Grain, scope_threads: str, *, rolled: bool = False) -> str:
    """Return the one scoped presence time every presence-backed series reads.

    ``scoped_attention`` is the scope's union time: a composite scope counts each slice
    once, an agent its evenly split thread slices; ``rolled`` adds the time the rollup
    holds before ``:focus_rollup_through`` to the raw time after it. ``scoped_visits`` are the
    scope's clipped visits, the observations behind that time.
    """
    predicate = presence_predicate(scope_threads)
    rollup = (f", {rolled_attention_cte(grain, scope_threads, presence_predicate(scope_threads, alias='m'))}"
              if rolled else "")
    rolled_rows = "\n        UNION ALL SELECT began,seconds FROM presence_rollup_scoped" if rolled else ""
    return f"""{presence_allocation_cte(grain, rolled=rolled)}{rollup}, scoped_attention AS (
        SELECT s.began,s.seconds FROM presence_live_segments s
        WHERE :scope_agent IS NULL AND EXISTS (
          SELECT 1 FROM presence_focus_spans v WHERE v.began<=s.began AND v.ended>=s.ended AND {predicate}
        )
        UNION ALL
        SELECT s.began,s.seconds FROM presence_thread_slices s
        WHERE :scope_agent IS NOT NULL AND s.thread_id IN (SELECT id FROM {scope_threads}){rolled_rows}
      ), scoped_visits AS (
        SELECT v.began FROM presence_intervals v WHERE v.began<v.ended AND {predicate}
      )"""


def presence_family_sql(grain: Grain, scope_threads: str, *, rolled: bool = False) -> str:
    """Return each scope's presence hours by period, with n the visits that began in it."""
    return f"""
      WITH {scoped_presence_cte(grain, scope_threads, rolled=rolled)}, hours AS (
        SELECT {grain.bucket('began')} AS period,SUM(seconds)/3600.0 AS value
        FROM scoped_attention WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ), visits AS (
        SELECT {grain.bucket('began')} AS period,COUNT(*) AS n
        FROM scoped_visits WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ) SELECT h.period,h.value,COALESCE(c.n,0) AS n FROM hours h LEFT JOIN visits c USING(period) ORDER BY h.period
    """


def active_family_sql(grain: Grain, scope_threads: str, outcomes: str, *, rolled: bool = False) -> str:
    """Return each scope's union-presence time and outcome count by period.

    A period with outcomes and no presence is served with zero time (``PresenceCoverage``);
    the caller withholds periods before coverage starts.
    """
    return f"""
      WITH {scoped_presence_cte(grain, scope_threads, rolled=rolled)}, active_periods AS (
        SELECT {grain.bucket('began')} AS period,SUM(seconds)/3600.0 AS active_hours
        FROM scoped_attention WHERE {grain.bucket('began')} IS NOT NULL GROUP BY period
      ), active_spine AS (
        SELECT period FROM active_periods UNION SELECT period FROM {outcomes}
      ) SELECT s.period,COALESCE(a.active_hours,0) AS active_hours,COALESCE(o.outcomes,0) AS outcomes
        FROM active_spine s LEFT JOIN active_periods a USING(period) LEFT JOIN {outcomes} o USING(period)
        ORDER BY s.period
    """
