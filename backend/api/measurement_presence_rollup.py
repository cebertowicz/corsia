"""The SQL that reads the focus-attention rollup (``api.presence_rollup``) for the scope in play.

A rollup row is the seconds of one 15-minute bucket during which exactly one set of
threads was focused. The raw allocator in ``api.measurement_presence`` derives the
same figures from the spans, so a read adds the two: this for time before
``:focus_rollup_through``, the raw tail from it on.

* A composite scope (All work, a project) counts a set's seconds once when any of its
  members satisfies the scope's presence predicate.
* An agent counts a set's seconds times its share of the set's distinct threads, the
  equal split ``presence_thread_slices`` makes, so agent rows still add to All work.
"""

from __future__ import annotations

from core.measurement_scope import Grain


def rolled_rows_cte(grain: Grain) -> str:
    """The user's rollup time per period and set: ``presence_rollup_rows`` rows of (began, set_id, seconds).

    ``began`` is the period's first instant. Each period reads its own stretch of the rollup's primary key
    and adds the rows there, so a read costs the rollup rows it sums and nothing per row beyond that. The
    stretch is also cut to ``[:focus_rollup_from, :focus_rollup_through)``.
    """
    stops = [f":gb{index + 1}" for index in range(len(grain.starts) - 1)] + [":gend"]
    periods = " UNION ALL ".join(f"SELECT :gb{index} AS began,{stop} AS stop" for index, stop in enumerate(stops))
    return f"""presence_rollup_periods AS ({periods}
      ), presence_rollup_rows AS (
        SELECT p.began,r.set_id,SUM(r.seconds) AS seconds
        FROM presence_rollup_periods p JOIN presence_focus_rollup r ON r.user_id=:user_id
          AND r.bucket>=MAX(p.began,:focus_rollup_from) AND r.bucket<MIN(p.stop,:focus_rollup_through)
        GROUP BY p.began,r.set_id
      )"""


def rolled_attention_cte(grain: Grain, scope_threads: str, member_predicate: str) -> str:
    """The scope's rollup time: ``presence_rollup_scoped`` rows of (began, seconds), one per period and set.

    ``member_predicate`` is ``presence_predicate`` over the alias ``m`` (a set's member).
    """
    return f"""{rolled_rows_cte(grain)}, presence_rollup_sets AS (
        SELECT m.set_id,MAX({member_predicate}) AS matched,COUNT(DISTINCT m.thread_id) AS threads,
          COUNT(DISTINCT CASE WHEN m.thread_id IN (SELECT id FROM {scope_threads}) THEN m.thread_id END) AS in_scope
        FROM presence_focus_set_members m WHERE m.set_id IN (SELECT set_id FROM presence_rollup_rows)
        GROUP BY m.set_id
      ), presence_rollup_scoped AS (
        SELECT r.began,r.seconds*CASE WHEN :scope_agent IS NULL THEN 1.0 ELSE 1.0*s.in_scope/s.threads END AS seconds
        FROM presence_rollup_rows r JOIN presence_rollup_sets s USING(set_id)
        WHERE (:scope_agent IS NULL AND s.matched=1) OR (:scope_agent IS NOT NULL AND s.in_scope>0)
      )"""


def rolled_root_cte(grain: Grain, scope_threads: str) -> str:
    """Each agent root's rollup time: ``presence_rollup_roots`` rows of (root_id, began, seconds), one per period and set.

    A set's seconds split evenly over its distinct threads; a root takes the share of its own.
    """
    return f"""{rolled_rows_cte(grain)}, presence_rollup_threads AS (
        SELECT m.set_id,COUNT(DISTINCT m.thread_id) AS threads FROM presence_focus_set_members m
        WHERE m.set_id IN (SELECT set_id FROM presence_rollup_rows) GROUP BY m.set_id
      ), presence_rollup_root_threads AS (
        SELECT m.set_id,s.root_id,COUNT(DISTINCT m.thread_id) AS threads
        FROM presence_focus_set_members m JOIN {scope_threads} s ON s.id=m.thread_id
        WHERE m.set_id IN (SELECT set_id FROM presence_rollup_rows) GROUP BY m.set_id,s.root_id
      ), presence_rollup_roots AS (
        SELECT x.root_id,r.began,r.seconds*1.0*x.threads/t.threads AS seconds
        FROM presence_rollup_rows r JOIN presence_rollup_root_threads x USING(set_id)
          JOIN presence_rollup_threads t USING(set_id)
      )"""
