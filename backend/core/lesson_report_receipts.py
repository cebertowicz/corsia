"""Read exact per-call indexed lesson offers; never substitute a Turn's final manifest."""
from __future__ import annotations

import json
from threading import Event
from typing import Any

from core.guidance_manifest import SKILLS_INDEX_SOURCE_ID
from core import lesson_report_offers as offers
from core.lesson_report_sql import json_row_chunk_lists, json_row_chunks


_USAGE_SQL = """
SELECT u.id,u.thread_id,u.turn_id,u.plan_node_id,u.plan_attempt_id,u.plan_commitment_id,
       u.context_manifest_id,u.created_at,u.model,m.harness,m.entries_json
FROM threads t JOIN llm_usage u ON u.thread_id=t.id
LEFT JOIN context_manifests m ON m.id=u.context_manifest_id
WHERE t.id=? AND t.user_id=? AND u.created_at>=? AND u.created_at<?
ORDER BY u.created_at,u.id
"""


def _lessons(entries_json: str | None) -> tuple[list[dict[str, Any]] | None, str | None]:
    if entries_json is None:
        return None, "per-call manifest unavailable"
    entries = json.loads(entries_json)
    if not isinstance(entries, list):
        return None, "manifest entries malformed"
    sources = [entry for entry in entries if isinstance(entry, dict)
               and entry.get("source_id") == SKILLS_INDEX_SOURCE_ID]
    if len(sources) != 1:
        return None, "unique installed Skills source unavailable"
    lessons = sources[0].get("lessons")
    if not isinstance(lessons, list):
        return None, "lesson metadata unavailable"
    if any(not isinstance(lesson, dict) or not isinstance(lesson.get("id"), str)
           or not lesson["id"] or not isinstance(lesson.get("behavioral_sha256"), str)
           or not lesson["behavioral_sha256"] for lesson in lessons):
        return None, "lesson metadata malformed"
    return lessons, None


def read_offers(
    db, *, user_id: str, thread_ids: tuple[str, ...], from_ts: str, to_ts: str,
) -> dict[str, Any]:
    """Read only existing same-user rows in a server-resolved thread scope.

    Thread ids are not authority: the join enforces current same-user membership.
    Missing-owner receipts need separately proven existing visibility, not a global
    manifest/plan scan. No installed boundary tuple is chosen as a call's raw version.
    """
    offers: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    calls = 0
    for thread_id in dict.fromkeys(thread_ids):
        for usage in db.execute(_USAGE_SQL, (thread_id, user_id, from_ts, to_ts)):
            calls += 1
            lessons, reason = _lessons(usage["entries_json"])
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            seen: set[tuple[str, str]] = set()
            for lesson in lessons or []:
                identity = lesson["id"], lesson["behavioral_sha256"]
                if identity in seen:
                    continue
                seen.add(identity)
                offers.append({
                    "kind": "lesson", "id": identity[0], "version": identity[1],
                    "offered": True, "usage_id": usage["id"], "thread_id": usage["thread_id"],
                    "turn_id": usage["turn_id"], "manifest_id": usage["context_manifest_id"],
                    "task_id": usage["plan_node_id"], "attempt_id": usage["plan_attempt_id"],
                    "commitment_id": usage["plan_commitment_id"], "created_at": usage["created_at"],
                    "model": usage["model"], "harness": usage["harness"],
                    "raw_version": None, "descriptor_tokens": None,
                    "version_reason": "per-call manifest records behavioral identity only",
                })
    return {"offers": offers, "calls": calls, "unavailable_calls": reasons}


_USAGE_GROUPS_SQL = (
    "SELECT u.context_manifest_id AS manifest_id,"
    "SUM(u.created_at>=? AND u.created_at<?) AS period_calls,"
    "MIN(CASE WHEN u.created_at>=? AND u.created_at<? THEN u.created_at||char(31)||u.id END) AS first_period,"
    "MIN(u.created_at||char(31)||u.id) AS first_call "
    "FROM threads t JOIN llm_usage u ON u.thread_id=t.id "
    "WHERE t.user_id=? AND t.id IN ({placeholders}) GROUP BY u.context_manifest_id"
)


def _identities(lessons: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """A call's distinct lesson identities in manifest order, as the per-call loop saw them."""
    return list(dict.fromkeys((lesson["id"], lesson["behavioral_sha256"]) for lesson in lessons))


def _classify_groups(groups: list[dict[str, Any]], entries: dict[Any, Any]):
    """Classify each distinct manifest once: its offered identities, period reasons and earliest calls."""
    identities: list[list[Any]] = []
    reasoned: list[tuple[str, str, int]] = []
    earliest: dict[tuple[str, str], tuple[str, str]] = {}
    period_calls = 0
    for group in groups:
        period_calls += group["period_calls"] or 0
        lessons, reason = _lessons(entries.get(group["manifest_id"]))
        if reason:
            if group["period_calls"]:
                reasoned.append((group["first_period"], reason, group["period_calls"]))
            continue
        first = tuple(group["first_call"].split("\x1f", 1))
        for order, identity in enumerate(_identities(lessons or [])):
            identities.append([group["manifest_id"], order, *identity])
            # An identity's retained offer is its earliest call over every manifest carrying it.
            if identity not in earliest or first < earliest[identity]:
                earliest[identity] = first
    return identities, reasoned, earliest, period_calls


def read_offer_evidence(
    db, *, user_id: str, thread_ids: tuple[str, ...], from_ts: str, to_ts: str,
    cancellation_event: Event | None = None,
) -> dict[str, Any]:
    """Scan scoped lifetime receipts once, retaining identities and period details separately.

    A call's outcome depends only on its manifest, so each distinct manifest is classified
    once. Calls whose manifest offers no lessons contribute counts only and are aggregated in
    SQL. The first offer of each identity (its earliest call) is built by SQLite as JSON chunks
    for retention; the in-period offers stay in SQLite as ``DeferredOffers``.
    """
    retained: dict[tuple[str, str], dict[str, Any]] = {}
    period_reasons: dict[str, int] = {}
    period_calls = 0
    owners = tuple(dict.fromkeys(thread_ids))
    all_identities: dict[tuple[str, int], list[Any]] = {}
    for offset in range(0, len(owners), offers.OWNER_CHUNK):
        chunk = owners[offset:offset + offers.OWNER_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        groups = list(json_row_chunks(
            db, _USAGE_GROUPS_SQL.format(placeholders=placeholders),
            (from_ts, to_ts, from_ts, to_ts, user_id, *chunk),
            ("manifest_id", "period_calls", "first_period", "first_call"), "manifest_id",
        ))
        manifest_ids = [group["manifest_id"] for group in groups if group["manifest_id"] is not None]
        entries = {
            row["id"]: row["entries_json"]
            for row in json_row_chunks(
                db, "SELECT id,entries_json FROM context_manifests WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(manifest_ids),), ("id", "entries_json"), "id",
            )
        } if manifest_ids else {}
        identities, reasoned, earliest, chunk_calls = _classify_groups(groups, entries)
        period_calls += chunk_calls
        for _first, reason, count in sorted(reasoned):
            period_reasons[reason] = period_reasons.get(reason, 0) + count
        if not identities:
            continue
        all_identities.update({(row[0], row[1]): row for row in identities})
        # Only those earliest calls are read, to build the retained offers.
        calls = json.dumps(sorted({call_id for _created_at, call_id in earliest.values()}))
        for rows in json_row_chunk_lists(
            db, offers.CALLS_SQL, (calls, json.dumps(identities), json.dumps(list(chunk)), user_id),
            offers.FIELDS, offers.ORDER,
        ):
            for offer in rows:
                identity = offer["id"], offer["version"]
                if offer["usage_id"] == earliest[identity][1]:
                    retained.setdefault(identity, offer)
    period_offers = offers.DeferredOffers(list(owners), list(all_identities.values()), user_id, from_ts, to_ts)
    period_offers.collect(db, cancellation_event=cancellation_event)
    return {"retained": list(retained.values()), "period": {"offers": period_offers,
            "calls": period_calls, "unavailable_calls": period_reasons}}


def offer_counts(period_offers: offers.DeferredOffers, identity: tuple[str, str]) -> dict[str, Any]:
    """Count distinct recorded calls/tasks/attempts, not successes or causal benefit."""
    counts = period_offers.counts.get(identity, {})
    return {
        "calls": counts.get("calls", 0), "tasks": counts.get("tasks", 0),
        "attempts": counts.get("attempts", 0), "calls_without_task": counts.get("calls_without_task", 0),
        "opportunity": None, "application": None, "efficacy": None,
        "reason": "indexed offers do not prove reading, use, or benefit",
    }


