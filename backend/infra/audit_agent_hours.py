"""Aggregate covered hourly usage without exposing individual threads."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


# Uncached input is what the fresh total leaves after output and cache writes,
# the same subtraction Measurement's key figures use.
_TOKEN_SUMS = (
    "coalesce(sum(records),0) AS calls, "
    "coalesce(sum(fresh_tokens),0)-coalesce(sum(completion_tokens),0) "
    "-coalesce(sum(cache_creation_tokens),0) AS prompt, "
    "coalesce(sum(completion_tokens),0) AS completion, "
    "coalesce(sum(cache_creation_tokens),0) AS cache_write, "
    "coalesce(sum(cache_read_tokens),0) AS cache_read"
)


def _coverage(db: sqlite3.Connection, metric: str, since: str, until: str,
              definition: str | None) -> tuple[set[str], dict]:
    start, end = datetime.fromisoformat(since), datetime.fromisoformat(until)
    hours = [start + timedelta(hours=i) for i in range(int((end - start).total_seconds() // 3600))]
    state = db.execute(
        "SELECT s.definition,s.built_through,d.hour FROM llm_usage_hourly_state s "
        "LEFT JOIN llm_usage_hourly_dirty d ON d.hour>=? AND d.hour<? WHERE s.id=1",
        (start.isoformat()[:13], end.isoformat()[:13]),
    ).fetchall()
    through = state[0][1] if state and definition and state[0][0] == definition else ""
    dirty = {row[2] for row in state if row[2] is not None}
    covered = {h.isoformat()[:13] for h in hours if h.isoformat()[:13] < through and h.isoformat()[:13] not in dirty}
    coverage = {"hours_in_window": len(hours), "hours_left_out": len(hours) - len(covered)}
    if metric == "agent_day_levels":
        days = sorted({h.date().isoformat() for h in hours})
        clean_days = {day for day in days if all(h.isoformat()[:13] in covered for h in hours if h.date().isoformat() == day)}
        coverage.update(days_in_window=len(days), days_left_out=len(days) - len(clean_days))
        covered = {hour for hour in covered if hour[:10] in clean_days}
    return covered, coverage


def read_agent_hours(db: sqlite3.Connection, metric: str, user_id: str, project_id: str,
                     since: str, until: str, definition: str | None, limit: int) -> tuple[list[dict], dict]:
    """Return aggregate rows and coverage for the caller's owned threads."""
    covered, coverage = _coverage(db, metric, since, until, definition)
    if not covered:
        return [], coverage
    placeholders = ",".join("?" for _ in covered)
    # spawned_by marks hidden workers; agent_enabled marks rostered agents.
    # Both describe current thread state, including archived threads.
    base = (
        "WITH scoped AS (SELECT h.thread_id,h.hour,h.vendor_account_id,h.records, "
        "h.fresh_tokens,h.completion_tokens,h.cache_creation_tokens,h.cache_read_tokens, "
        "CASE WHEN t.spawned_by IS NOT NULL THEN 'temporary_worker' "
        "WHEN coalesce(t.agent_enabled,0)=1 THEN 'persistent_agent' ELSE 'other_chat' END AS kind, "
        "CASE WHEN t.project_id IS NULL THEN 'no_project' WHEN t.project_id=? "
        "THEN 'caller_project' ELSE 'another_project' END AS location, "
        "CASE WHEN h.vendor_account_id IS NULL THEN CASE WHEN instr(h.model,'/')>0 "
        "THEN substr(h.model,1,instr(h.model,'/')-1) ELSE h.model END END AS provider "
        "FROM llm_usage_hourly h JOIN threads t ON t.id=h.thread_id "
        f"WHERE h.hour IN ({placeholders}) AND t.user_id=?) "
    )
    params = (project_id, *sorted(covered), user_id)
    if metric == "agent_hours":
        sql = base + (
            ", per_hour AS (SELECT thread_id,hour,kind,vendor_account_id,provider, " + _TOKEN_SUMS +
            " FROM scoped GROUP BY thread_id,hour,kind,vendor_account_id,provider), "
            "account_rows AS (SELECT vendor_account_id AS account_id,provider, "
            "count(*) AS agent_hours,count(DISTINCT thread_id) AS agents, "
            "count(DISTINCT CASE WHEN kind='temporary_worker' THEN thread_id END) AS temporary_workers, "
            "sum(kind='temporary_worker') AS temporary_worker_hours, "
            "sum(calls) AS calls,sum(prompt) AS prompt,sum(completion) AS completion, "
            "sum(cache_write) AS cache_write,sum(cache_read) AS cache_read "
            "FROM per_hour GROUP BY vendor_account_id,provider), "
            "all_hours AS (SELECT thread_id,hour,kind,sum(calls) AS calls,sum(prompt) AS prompt, "
            "sum(completion) AS completion,sum(cache_write) AS cache_write,sum(cache_read) AS cache_read "
            "FROM per_hour GROUP BY thread_id,hour,kind), "
            "all_row AS (SELECT NULL AS account_id,NULL AS provider,count(*) AS agent_hours, "
            "count(DISTINCT thread_id) AS agents, "
            "count(DISTINCT CASE WHEN kind='temporary_worker' THEN thread_id END) AS temporary_workers, "
            "coalesce(sum(kind='temporary_worker'),0) AS temporary_worker_hours, "
            "coalesce(sum(calls),0) AS calls,coalesce(sum(prompt),0) AS prompt, "
            "coalesce(sum(completion),0) AS completion,coalesce(sum(cache_write),0) AS cache_write, "
            "coalesce(sum(cache_read),0) AS cache_read FROM all_hours) "
            "SELECT 'account' AS row_type,a.account_id,v.name AS account_name,a.provider, "
            "a.agent_hours,a.agents,a.temporary_workers,a.temporary_worker_hours,a.calls, "
            "a.prompt,a.completion,a.cache_write,a.cache_read FROM account_rows a "
            "LEFT JOIN vendor_accounts v ON v.id=a.account_id UNION ALL "
            "SELECT 'all',a.account_id,NULL,a.provider,a.agent_hours,a.agents, "
            "a.temporary_workers,a.temporary_worker_hours,a.calls,a.prompt,a.completion, "
            "a.cache_write,a.cache_read FROM all_row a ORDER BY row_type,account_id,provider LIMIT ?"
        )
    else:
        sql = base + (
            ", per_day AS (SELECT thread_id,substr(hour,1,10) AS day,kind,location, "
            "count(DISTINCT hour) AS hours, " + _TOKEN_SUMS +
            " FROM scoped GROUP BY thread_id,day,kind,location), "
            "banded AS (SELECT *,CASE WHEN hours<=2 THEN '1-2' WHEN hours<=6 THEN '3-6' "
            "WHEN hours<=12 THEN '7-12' WHEN hours<=18 THEN '13-18' ELSE '19-24' END AS band "
            "FROM per_day) SELECT kind,location,band,count(*) AS agent_days,sum(hours) AS agent_hours, "
            "sum(calls) AS calls,sum(prompt) AS prompt,sum(completion) AS completion, "
            "sum(cache_write) AS cache_write,sum(cache_read) AS cache_read "
            "FROM banded GROUP BY kind,location,band ORDER BY kind,location, "
            "CASE band WHEN '1-2' THEN 1 WHEN '3-6' THEN 2 WHEN '7-12' THEN 3 "
            "WHEN '13-18' THEN 4 ELSE 5 END LIMIT ?"
        )
    return [dict(row) for row in db.execute(sql, (*params, limit)).fetchall()], coverage
