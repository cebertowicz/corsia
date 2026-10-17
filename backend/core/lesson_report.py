"""One read-only report projection shared by the tool and HTTP adapters.

Adapters resolve the existing user-visible scope and source files first. Neither a
cursor nor a lesson path grants visibility or access to a missing owner's plan.
"""
from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from threading import Event
from typing import Any

from core.lesson_report_boundaries import read_boundaries
from core.lesson_report_inventory import build_inventory
from core.lesson_report_offers import PAGE_SIZE, DeferredOffers, DetailRequest, snapshot_fingerprint
from core.lesson_report_reads import read_bodies
from core.lesson_report_receipts import offer_counts, read_offer_evidence
from core.lesson_trials import parse_instant
from plans import public as plans

# A stale watch is omitted from list reads, never retired or otherwise mutated.
WATCH_HIDE_AFTER = timedelta(days=30)
# The lifecycle states a list read can be narrowed to; their rows carry a one-line statement.
LISTABLE_STATES = ("watch", "trial")


@dataclass(frozen=True)
class ReportScope:
    """Server-resolved current membership and the existing evidence window."""

    user_id: str
    thread_ids: tuple[str, ...]
    identity: str
    from_ts: str
    to_ts: str
    owner_ids_user_resolved: bool = False


def _history(db, scope: ReportScope) -> tuple[list[dict[str, Any]], list[str]]:
    """Read explicit owners; retain the check for non-snapshot-supplied scopes."""
    if scope.owner_ids_user_resolved:
        return (plans.lesson_review_history(db, owner_thread_ids=scope.thread_ids), []) if scope.thread_ids else ([], [])
    visible: list[str] = []
    missing: list[str] = []
    for owner in dict.fromkeys(scope.thread_ids):
        row = db.execute("SELECT user_id FROM threads WHERE id=?", (owner,)).fetchone()
        if row is None:
            missing.append(owner)
        elif row["user_id"] != scope.user_id:
            raise PermissionError("owner is outside the resolved user scope")
        else:
            visible.append(owner)
    return plans.lesson_review_history(db, owner_thread_ids=tuple(visible)) if visible else [], missing


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _position(cursor: str | None, fingerprint: str, query: dict[str, Any]) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or len(cursor) > 1024:
        raise ValueError("invalid report cursor; refresh the report")
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid report cursor; refresh the report") from None
    if not isinstance(value, dict) or set(value) != {"scope", "snapshot", "offset"}:
        raise ValueError("invalid report cursor; refresh the report")
    if value["scope"] != _digest(query):
        raise ValueError("report scope differs; use the intended filters without cursor")
    if value["snapshot"] != fingerprint:
        raise ValueError("report changed; refresh the report without cursor")
    offset = value["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid report cursor; refresh the report")
    return offset


def _cursor(fingerprint: str, query: dict[str, Any], offset: int) -> str:
    return base64.urlsafe_b64encode(_canonical({
        "scope": _digest(query), "snapshot": fingerprint, "offset": offset,
    })).decode()


def _summary(row: dict[str, Any], offers: DeferredOffers, body_reads: int) -> dict[str, Any]:
    """One row summary from its period offers and in-period body-read count."""
    summary = {key: row[key] for key in (
        "kind", "id", "version", "name", "activated", "state", "source_status", "owner_ids",
        "due", "review_after", "open_reviews", "lifecycle_reason",
        "review_due", "due_work", "review_due_unknown",
    )} | {"indexed": offer_counts(offers, (row["id"], row["version"])),
          "source_scope": (row["current_source"] or {}).get("source_scope"),
          "source_scope_reason": "recorded source scope unavailable" if not row["current_source"] else None,
          "body_reads": body_reads,
          "next_call": (
        f"lesson_report(lesson_id={json.dumps(row['id'])}, version={json.dumps(row['version'])})"
    )}
    if "correction_status" in row:
        summary.update({key: row[key] for key in ("correction_status", "recurrence_count")})
    statement = (row["current_source"] or {}).get("statement")
    if row.get("state") in LISTABLE_STATES and statement:
        summary["statement"] = statement
    return summary


def _page_summaries(page: list[dict[str, Any]], period: dict[str, Any],
                    reads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summaries for one page only, grouping reads by exact identity once."""
    reads_by_identity = Counter((read["id"], read["version"]) for read in reads) if page else Counter()
    return [_summary(row, period["offers"], reads_by_identity[(row["id"], row["version"])]) for row in page]


def _correction_inventory(
    inventory: list[dict[str, Any]], facts: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Attach collected correction facts; list filtering follows detail and coverage."""
    for row in inventory:
        if "originating_subjects" not in row:
            continue
        found = [
            {"subject": subject, **facts[subject]}
            for subject in row.get("originating_subjects", ()) if subject in facts
        ]
        linked = [fact for fact in found if "unreadable" not in fact]
        row["correction_status"] = ("linked" if linked else "unreadable" if found
                                    else "no correction")
        row["correction_facts"] = linked
        row["recurrence_count"] = len(linked)
    return inventory


def _list_inventory(inventory: list[dict[str, Any]], now: str,
                    state: str | None = None) -> list[dict[str, Any]]:
    """Hide stale, un-recurred watches only from list and preview pages; keep only ``state`` if given."""
    instant = parse_instant(now)
    cutoff = instant - WATCH_HIDE_AFTER if instant else None
    visible = []
    for row in inventory:
        if state is not None and row.get("state") != state:
            continue
        activated = min(
            (value for review in row.get("reviews", ())
             if (value := parse_instant(review.get("opened_at"))) is not None), default=None,
        )
        first_anchor = min(
            (value for fact in row.get("correction_facts", ())
             if (value := parse_instant(fact["ts"])) is not None), default=None,
        )
        created = activated or first_anchor
        has_recent_anchor = any(
            cutoff is not None and (parse_instant(fact["ts"]) or cutoff) > cutoff
            for fact in row.get("correction_facts", ())
        )
        if row.get("state") == "watch" and cutoff and created and created <= cutoff and not has_recent_anchor:
            continue
        visible.append(row)
    return visible


def collect_report(db, *, scope: ReportScope, sources: list[dict[str, Any]],
                   detail: DetailRequest | None = None,
                   cancellation_event: Event | None = None) -> dict[str, Any]:
    """Materialize scoped database evidence without Git or external work.

    Period offers stay in SQLite as ``DeferredOffers``: counts, the fingerprint text and, for
    the ``detail`` lesson, the offers a page of it can show.
    """
    history, missing_owners = _history(db, scope)
    boundaries = read_boundaries(db, user_id=scope.user_id, thread_ids=scope.thread_ids)
    offers = read_offer_evidence(db, user_id=scope.user_id, thread_ids=scope.thread_ids,
                                 from_ts=scope.from_ts, to_ts=scope.to_ts,
                                 cancellation_event=cancellation_event)
    period = offers["period"]
    if detail is not None:
        # A row lists only reviews from this history, each node once, ahead of its offers.
        period["offers"].prefetch(db, detail, reviews_at_most=len(history))
    retained = offers["retained"] + boundaries
    bodies = read_bodies(db, user_id=scope.user_id, thread_ids=scope.thread_ids,
                         facts=sources + retained)
    from core.lesson_corrections import correction_facts, placement_check_counts, source_subjects

    # The adapter passes no sources here and joins corrections after reading lesson files.
    subjects = source_subjects(sources)
    return {"history": history, "missing_owners": missing_owners, "boundaries": boundaries,
            "retained": retained, "period": period, "bodies": bodies,
            "correction_facts": correction_facts(db, subjects),
            "placement_checks": placement_check_counts(db, scope.thread_ids)}


def detail_request(lesson_id: str | None, version: str | None, cursor: str | None,
                   evidence: str | None) -> DetailRequest | None:
    """The lesson page a caller asked for, so collection can read its offers inside the snapshot."""
    if lesson_id is None or evidence is not None:
        return None
    return DetailRequest(lesson_id, version, _cursor_offset(cursor))


def _detail_page(result: dict[str, Any], *, row: dict[str, Any], period: dict[str, Any],
                 reads: list[dict[str, Any]], boundaries: list[dict[str, Any]], offset: int,
                 fingerprint: str, query: dict[str, Any]) -> dict[str, Any]:
    """Page references for one exact behavior without expanding the list surface."""
    offers, identity = period["offers"], (row["id"], row["version"])
    reviews = [{"kind": "review", "value": review} for review in row["reviews"]]
    bodies = [{"kind": "body_read", "value": read} for read in reads
              if (read["id"], read["version"]) == identity]
    installed = [{"kind": "installed_boundary", "value": fact} for fact in boundaries
                 if (fact["id"], fact["version"]) == identity]

    def indexed(start: int, end: int) -> list[dict[str, Any]]:
        return [{"kind": "indexed", "value": offer} for offer in offers.page(identity, start, end)]

    segments = [(len(reviews), lambda start, end: reviews[start:end]), (offers.total(identity), indexed),
                (len(bodies), lambda start, end: bodies[start:end]),
                (len(installed), lambda start, end: installed[start:end])]
    total = sum(length for length, _page in segments)
    if offset > total:
        raise ValueError("invalid detail position; refresh the report")
    page: list[dict[str, Any]] = []
    position = 0
    for length, fetch in segments:
        start, end = max(offset, position) - position, min(offset + PAGE_SIZE, position + length) - position
        if end > start:
            page.extend(fetch(start, end))
        position += length
    result["detail"] = _summary(row, offers, len(bodies)) | {
        "current_source": row["current_source"],
        **({"correction_facts": row["correction_facts"]} if "correction_facts" in row else {}),
        "references": page,
        "reference_count": total, "source_bytes": None,
        "carrying_cost": {"descriptor_tokens": None, "body_tokens": None,
                          "reason": "period calls do not bind descriptor/body estimates to an exact installed version"},
        "source_bytes_reason": "historical bytes require an exact recorded file object",
    }
    result["next_cursor"] = _cursor(fingerprint, query, offset + PAGE_SIZE) if offset + PAGE_SIZE < total else None
    return result


def _cursor_offset(cursor: Any) -> int:
    """The offset a cursor claims; validation still happens once the snapshot is fingerprinted."""
    try:
        offset = json.loads(base64.urlsafe_b64decode(cursor))["offset"]
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return 0
    return offset if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0 else 0


def _coverage(inventory: list[dict[str, Any]], period: dict[str, Any], reads: list[dict[str, Any]],
              unidentified: list[dict[str, Any]], missing_owners: Any, bodies: dict[str, Any]) -> dict[str, Any]:
    """The report's evidence-coverage counts, before adapter source coverage is merged in."""
    return {
        "due_versions": sum(row["due_work"] is True for row in inventory),
        "known_activated_versions": sum(row["activated"] is True for row in inventory),
        "activation_unknown_versions": sum(row["activated"] is None for row in inventory),
        "captured_versions": sum(row["kind"] == "capture" for row in inventory),
        "full_activated_population": None,
        "owner_scope": "current same-user membership; orphan-owner coverage unavailable",
        "missing_owner_scopes": missing_owners,
        "deleted_history_complete": False, "extension_history_complete": False,
        "period_calls": period["calls"], "unavailable_period_calls": period["unavailable_calls"],
        "period_body_reads": len(reads), "unidentified_body_reads": len(unidentified),
        "unavailable_body_reads": bodies["unavailable_reads"], "body_read_coverage": bodies["coverage"],
        "rates": None, "rates_reason": "full activated population and opportunity are not proven",
    }


def read_report(
    db, *, scope: ReportScope, sources: list[dict[str, Any]], now: str,
    cursor: str | None = None, lesson_id: str | None = None,
    version: str | None = None, preview: bool = False,
    blob_reader=None, evidence: str | None = None,
    collected: dict[str, Any] | None = None,
    source_coverage: dict[str, Any] | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Read due-first current lifecycle plus period evidence, without writes.

    A complete current-owner walk is not a complete deleted-owner population.
    The semantic snapshot is recomputed for each page, not stored in a registry;
    changed evidence/source/review facts require a refresh rather than mixed pages.
    Adapters collect database evidence inside their read snapshot, then close
    the transaction before resolving immutable Git objects. Source bytes are hashed inputs.
    """
    if evidence not in {None, "unidentified_reads"} or (evidence and lesson_id is not None):
        raise ValueError("choose an exact lesson or evidence=unidentified_reads")
    if state is not None and state not in LISTABLE_STATES:
        raise ValueError("state must be watch or trial")
    from core.lesson_report_reads import identify_recorded_reads

    data = collected if collected is not None else collect_report(
        db, scope=scope, sources=sources, detail=detail_request(lesson_id, version, cursor, evidence))
    history, missing_owners = data["history"], data["missing_owners"]
    boundaries, retained, period = data["boundaries"], data["retained"], data["period"]
    correction_facts = data["correction_facts"]
    placement_checks = data["placement_checks"]
    bodies = data["bodies"]
    bodies = bodies | {"reads": identify_recorded_reads(bodies["reads"], blob_reader, sources)}
    reads = [read for read in bodies["reads"] if scope.from_ts <= read["created_at"] < scope.to_ts]
    unidentified = [read for read in reads if read["version"] is None]
    inventory = build_inventory(history=history, sources=sources,
                                retained=retained + bodies["reads"], now=now)
    inventory = _correction_inventory(inventory, correction_facts)
    coverage = _coverage(inventory, period, reads, unidentified, missing_owners, bodies)
    if placement_checks["fires"] or any("originating_subjects" in row for row in inventory):
        coverage["placement_checks"] = placement_checks
    coverage.update(source_coverage or {})
    snapshot = {"rows": inventory, "period": period, "reads": reads, "coverage": coverage}
    fingerprint = snapshot_fingerprint(snapshot, _canonical)
    query = {**scope.__dict__, "lesson_id": lesson_id, "version": version,
             "preview": preview, "evidence": evidence} | ({"state": state} if state else {})
    offset = _position(cursor, fingerprint, query)
    result: dict[str, Any] = {
        "scope": scope.identity, "period": {"from": scope.from_ts, "to": scope.to_ts,
                                          "basis": "completed weeks; current week excluded; end exclusive"},
        "snapshot": fingerprint, "coverage": coverage,
    }
    if evidence == "unidentified_reads":
        if offset > len(unidentified):
            raise ValueError("invalid evidence position; refresh the report")
        result["rows"] = unidentified[offset:offset + 20]
        result["next_cursor"] = (_cursor(fingerprint, query, offset + 20)
                                 if offset + 20 < len(unidentified) else None)
        return result
    result["unidentified_reads"] = unidentified[:5]
    result["unidentified_next_call"] = ('lesson_report(evidence="unidentified_reads")' if unidentified else None)
    if lesson_id is not None:
        selected = [row for row in inventory if row["id"] == lesson_id and (version is None or row["version"] == version)]
        if len(selected) != 1:
            result.update({"detail": None, "reason": "exact lesson/version not unique in this view",
                           "next_call": "lesson_report(scope=\"all\")"})
            return result
        return _detail_page(result, row=selected[0], period=period, reads=reads, boundaries=boundaries,
                            offset=offset, fingerprint=fingerprint, query=query)
    visible_inventory = _list_inventory(inventory, now, state)
    limit = 5 if preview else 20
    if offset > len(visible_inventory):
        raise ValueError("invalid report position; refresh the report")
    result["rows"] = _page_summaries(visible_inventory[offset:offset + limit], period, reads)
    result["known_versions"] = len(visible_inventory)
    result["next_cursor"] = (_cursor(fingerprint, query, offset + limit)
                             if offset + limit < len(visible_inventory) else None)
    return result
