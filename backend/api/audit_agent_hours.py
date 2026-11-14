"""Aggregate exact hourly usage without exposing individual threads."""

from __future__ import annotations

import sqlite3

from api.measurement_token_sql import ROLLUP_TOKEN_CLASS_SQL
from api.usage_rollup import rollup_bound, usage_source


def read_agent_hours(db: sqlite3.Connection, metric: str, user_id: str, project_id: str,
                     since: str, until: str, limit: int) -> list[dict]:
    """Return aggregate rows for the caller's owned threads."""
    token_classes = dict(ROLLUP_TOKEN_CLASS_SQL)
    token_sums = ", ".join(
        ["coalesce(sum(u.records),0) AS calls"] +
        [f"coalesce({token_classes[name]},0) AS {alias}" for name, alias in
         (("input", "prompt"), ("output", "completion"), ("cache_write", "cache_write"), ("cache_read", "cache_read"))]
    )
    from_ts, to_ts = since[:13], until[:13]
    params = {"project_id": project_id, "user_id": user_id, "from_ts": from_ts, "to_ts": to_ts,
              "rollup_through": rollup_bound(db, from_ts, to_ts), "limit": limit}
    # Current thread state includes archived threads; missing threads are excluded.
    base = (
        "WITH user_threads AS (SELECT id FROM threads WHERE user_id=:user_id), "
        "scoped AS (SELECT u.thread_id,u.hour,u.vendor_account_id,u.records, "
        "u.fresh_tokens,u.completion_tokens,u.cache_creation_tokens,u.cache_read_tokens, "
        "CASE WHEN t.spawned_by IS NOT NULL THEN 'temporary_worker' "
        "WHEN coalesce(t.agent_enabled,0)=1 THEN 'persistent_agent' ELSE 'other_chat' END AS kind, "
        "CASE WHEN t.project_id IS NULL THEN 'no_project' WHEN t.project_id=:project_id "
        "THEN 'caller_project' ELSE 'another_project' END AS location, "
        "CASE WHEN u.vendor_account_id IS NULL THEN CASE WHEN instr(u.model,'/')>0 "
        "THEN substr(u.model,1,instr(u.model,'/')-1) ELSE u.model END END AS provider "
        f"FROM {usage_source('user_threads')} u JOIN threads t ON t.id=u.thread_id) "
    )
    if metric == "agent_hours":
        sql = base + (
            ", per_hour AS (SELECT thread_id,hour,kind,vendor_account_id,provider, " + token_sums +
            " FROM scoped u GROUP BY thread_id,hour,kind,vendor_account_id,provider), "
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
            "a.cache_write,a.cache_read FROM all_row a ORDER BY row_type,account_id,provider LIMIT :limit"
        )
    else:
        sql = base + (
            ", per_day AS (SELECT thread_id,substr(hour,1,10) AS day,kind,location, "
            "count(DISTINCT hour) AS hours, " + token_sums +
            " FROM scoped u GROUP BY thread_id,day,kind,location), "
            "banded AS (SELECT kind,location,hours,calls,prompt,completion,cache_write,cache_read, "
            "CASE WHEN hours<=2 THEN '1-2' WHEN hours<=6 THEN '3-6' "
            "WHEN hours<=12 THEN '7-12' WHEN hours<=18 THEN '13-18' ELSE '19-24' END AS band "
            "FROM per_day) SELECT kind,location,band,count(*) AS agent_days,sum(hours) AS agent_hours, "
            "sum(calls) AS calls,sum(prompt) AS prompt,sum(completion) AS completion, "
            "sum(cache_write) AS cache_write,sum(cache_read) AS cache_read "
            "FROM banded GROUP BY kind,location,band ORDER BY kind,location, "
            "CASE band WHEN '1-2' THEN 1 WHEN '3-6' THEN 2 WHEN '7-12' THEN 3 "
            "WHEN '13-18' THEN 4 ELSE 5 END LIMIT :limit"
        )
    return [dict(row) for row in db.execute(sql, params).fetchall()]
