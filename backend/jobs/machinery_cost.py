"""Record daily machinery facts without adding work to an agent turn.

The one guidance read per run scans the table: 45,226 rows at the census and
452,260 at ten times that volume. Tool reads seek each thread's day range.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

from loguru import logger

from api.measurement_token_sql import FRESH_TOKEN_SQL
from core.machinery_cost import DAY_EVENT, RECORD_EVENT
from core.thread_lifecycle import LifecycleConfig
from infra.database import get_db, write_transaction
from infra.outcome_events import record_outcome_event
from tools.catalog import TOOL_CATALOG, normalize_tool_name

TOOL_ROWS_SQL = """
    SELECT id, COALESCE(json_extract(metadata, '$.tool_name'), json_extract(metadata, '$.name')) AS name,
           json_extract(metadata, '$.mcp_app') AS mcp_app,
           json_extract(metadata, '$.tool_result_byte_count') AS recorded_bytes,
           length(CAST(content AS BLOB)) AS content_bytes
    FROM messages INDEXED BY idx_messages_session
    WHERE thread_id=? AND role='tool' AND created_at>=? AND created_at<?
"""


def _guidance_for_days(db, missing):
    """Scan guidance once and collect the reads attributable to each thread-day."""
    guidance: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    no_thread = 0
    wanted = {str(day) for day in missing}
    for row in db.execute(
        "SELECT thread_id, byte_count, partial, created_at FROM guidance_loads "
        "WHERE lesson_id IS NULL AND created_at>=? AND created_at<?",
        (str(missing[0]), str(missing[-1] + timedelta(days=1))),
    ):
        day = str(row["created_at"])[:10]
        if day not in wanted:
            continue
        if row["thread_id"] is None:
            no_thread += 1
            continue
        item = guidance[(day, row["thread_id"])]
        item[0] += 1
        if row["partial"]:
            item[2] += 1
        else:
            item[1] += row["byte_count"]
    return guidance, no_thread


def run_daily(*, now: datetime | None = None, config: LifecycleConfig | None = None) -> int:
    """Write missing closed days oldest first; a marker and its facts commit together."""
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    retention = (config or LifecycleConfig()).tool_trim_after_days
    days = [today - timedelta(days=n) for n in range(min(28, max(0, retention - 1)), 0, -1)]
    db = get_db()
    try:
        missing = [day for day in days if not db.execute(
            "SELECT 1 FROM outcome_events WHERE dedupe_key=?", (f"{DAY_EVENT}:{day}",)
        ).fetchone()]
        if not missing:
            return 0
        guidance, no_thread = _guidance_for_days(db, missing)
        threads = db.execute("SELECT id, project_id FROM threads").fetchall()
        written = 0
        skill_reads = partial_reads = 0
        for day in missing:
            start, end = str(day), str(day + timedelta(days=1))
            tokens = {row["thread_id"]: int(row["tokens"] or 0) for row in db.execute(
                f"SELECT u.thread_id, SUM({FRESH_TOKEN_SQL}) AS tokens FROM llm_usage u "
                "WHERE u.created_at>=? AND u.created_at<? GROUP BY u.thread_id", (start, end),
            )}
            records = []
            fallback = []
            day_skill_reads = day_partial_reads = 0
            for thread in threads:
                counts: Counter[str] = Counter()
                for row in db.execute(TOOL_ROWS_SQL, (thread["id"], start, end)):
                    name = normalize_tool_name(row["name"])
                    bucket = "connected_app" if row["mcp_app"] else (
                        TOOL_CATALOG[name].category if name in TOOL_CATALOG else "ungrouped"
                    )
                    size = row["recorded_bytes"]
                    if size is None:
                        size = row["content_bytes"]
                        fallback.append(row["id"])
                    counts[f"c_{bucket}"] += 1
                    counts[f"b_{bucket}"] += size or 0
                thread_tokens = tokens.get(thread["id"], 0)
                if not counts and not thread_tokens:
                    continue
                skills, skill_bytes, partial = guidance.get((start, thread["id"]), (0, 0, 0))
                day_skill_reads += skills
                day_partial_reads += partial
                counts.update({"skill_count": skills, "skill_bytes": skill_bytes})
                attrs = {**counts, "fresh_tokens": str(thread_tokens)}
                records.append((thread, attrs))
            marker = f"{DAY_EVENT}:{day}"
            stamp = datetime.combine(day, time(12), timezone.utc).isoformat()
            with write_transaction(db, scope="record_machinery_day") as conn:
                if conn.execute("SELECT 1 FROM outcome_events WHERE dedupe_key=?", (marker,)).fetchone():
                    continue
                for thread, attrs in records:
                    record_outcome_event(conn, dedupe_key=f"{RECORD_EVENT}:{day}:{thread['id']}",
                        event_name=RECORD_EVENT, source="corsia", provenance="instrumented", ts=stamp,
                        agent_thread_id=thread["id"], project_id=thread["project_id"],
                        attribution="caller", attributes=attrs)
                record_outcome_event(conn, dedupe_key=marker, event_name=DAY_EVENT, source="corsia",
                    provenance="instrumented", ts=stamp, attribution="none")
            written += 1
            skill_reads += day_skill_reads
            partial_reads += day_partial_reads
            if fallback:
                logger.info("machinery_cost: day={} rows without a stored byte count: {} (first: {})",
                            day, len(fallback), fallback[:5])
        logger.info("machinery_cost: days={} skill reads={} partial={} whole={} guidance rows without thread={}",
                    written, skill_reads, partial_reads, skill_reads - partial_reads, no_thread)
        return written
    finally:
        db.close()
