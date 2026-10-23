"""Conservative joins of genuine read_file results to exact guidance loads.

No time proximity or current-file fallback. A guidance load has no invocation
id, so repeated calls or loads for one thread/Turn/absolute path stay ambiguous.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from core import tool_result_binding
from core.tool_result_report import canonical_result_reasons
from core.lesson_report_boundaries import identity_for_raw
from core.lesson_report_files import recorded_source
from core.lesson_report_sql import json_row_chunks, read_file_metadata_predicate
from core.tool_result_projection import tool_result_reference
from tools.catalog import normalize_tool_name
from tools.tool_surface import resolve_dispatch


def _read_call(row) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        metadata = tool_result_binding.metadata_object(row["metadata"])
    except ValueError:
        return None, None
    name, arguments, _dispatcher = resolve_dispatch(metadata.get("name", ""), metadata.get("arguments"))
    if normalize_tool_name(name) != "read_file" or not isinstance(arguments, dict):
        return None, metadata
    return arguments, metadata


# Canonical result integrity and predecessor validation belong to the public
# report reader, which batches its own authoritative predecessors.

def _joined_read(db, row, metadata, load, facts) -> tuple[dict[str, Any] | None, str | None]:
    identity = identity_for_raw(facts, digest=load["raw_sha256"], path=load["path"])
    source = None
    identity_reason = None
    if identity is None:
        identity_reason = "workspace guidance history unavailable"
        identity = {"kind": "guidance", "id": None, "version": None}
    record = identity | {
        "identity_reason": identity_reason,
        "offered": False, "evidence_kind": "body read", "thread_id": row["thread_id"],
        "turn_id": metadata["turn_id"], "message_id": row["id"], "guidance_load_id": load["id"],
        "path": load["path"], "raw_sha256": load["raw_sha256"], "git_blob_oid": load["git_blob_oid"],
        "partial": bool(load["partial"]), "byte_count": load["byte_count"], "created_at": load["created_at"],
        "task_id": load["node_id"], "attempt_id": None,
        "task_reason": "receipt task attribution" if load["node_id"] else "read has no task attribution",
        "trial_variant": load["variant"],
        "trial_lesson_id": load["lesson_id"],
        "trial_behavioral_sha256": load["lesson_behavioral_sha256"],
        "checkpoint_generation_id": load["checkpoint_generation_id"],
        "provider_consumed": None, "application": None, "body_tokens": None,
        "body_tokens_reason": "no exact token allocation recorded; body remains history accounting",
        "result_reference": tool_result_reference(
            thread_id=row["thread_id"], message_id=row["id"],
            sha256=metadata["tool_result_sha256"], byte_count=metadata["tool_result_byte_count"],
        ),
    }
    if source is not None:
        record["recorded_source"] = source
    return record, None


def _reading_each_object_once(blob_reader):
    """Wrap the public reader so one report call reads an exact (oid, sha) once.

    The memo holds the reader's own (bytes, reason) answer, never a parsed source:
    every receipt still parses its bytes under its own path. It dies with the call.
    """
    if blob_reader is None:
        return None
    answers: dict[tuple[Any, Any], Any] = {}

    def read(oid, sha):
        try:
            key = (oid, sha)
            hash(key)
        except TypeError:
            return blob_reader(oid, sha)
        if key not in answers:
            answers[key] = blob_reader(oid, sha)
        return answers[key]

    return read


def identify_recorded_reads(reads: list[dict[str, Any]], blob_reader,
                            facts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Resolve immutable objects only after the adapter releases its read snapshot."""
    blob_reader = _reading_each_object_once(blob_reader)
    result = []
    for record in reads:
        if record["version"] is not None:
            result.append(record)
            continue
        identity = identity_for_raw(facts or [], digest=record["raw_sha256"], path=record["path"])
        if identity is not None:
            result.append(record | identity | {"identity_reason": None})
            continue
        source, reason = recorded_source(blob_reader, record)
        if source is None:
            result.append(record | {"identity_reason": reason})
        else:
            result.append(record | {key: source[key] for key in ("kind", "id", "version")} |
                          {"identity_reason": None, "recorded_source": source})
    return result


def _candidate_groups(db, user_id: str, thread_ids: tuple[str, ...], unavailable: Counter[str]):
    candidates: dict[tuple[str, str, str], list[tuple[Any, dict[str, Any]]]] = defaultdict(list)
    unique_threads = tuple(dict.fromkeys(thread_ids))
    for offset in range(0, len(unique_threads), 499):
        chunk = unique_threads[offset:offset + 499]
        placeholders = ",".join("?" for _ in chunk)
        rows = json_row_chunks(
            db,
            "SELECT m.id AS id,m.thread_id AS thread_id,m.metadata AS metadata,m.seq AS seq "
            "FROM threads t JOIN messages m ON m.thread_id=t.id "
            "WHERE t.user_id=? AND t.id IN (" + placeholders + ") AND m.role='tool' "
            "AND " + read_file_metadata_predicate("m.metadata"),
            (user_id, *chunk), ("id", "thread_id", "metadata"), "thread_id,seq,id",
        ) if chunk else ()
        for row in rows:
            arguments, metadata = _read_call(row)
            if arguments is None or metadata is None:
                continue
            path, turn = arguments.get("path"), metadata.get("turn_id")
            if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(turn, str) or not turn:
                unavailable["absolute path or Turn lineage unavailable"] += 1
                continue
            candidates[(str(row["thread_id"]), turn, path)].append((row, metadata))
    return candidates


_LOAD_COLUMNS = (
    "id", "thread_id", "turn_id", "path", "raw_sha256", "git_blob_oid", "delivered_sha256",
    "byte_count", "partial", "node_id", "variant", "checkpoint_generation_id", "lesson_id",
    "lesson_behavioral_sha256", "created_at",
)


def _loads_by_group(db, candidates):
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    if not candidates:
        return grouped
    wanted = json.dumps([list(pair) for pair in dict.fromkeys((thread, turn) for thread, turn, _path in candidates)])
    for load in json_row_chunks(
        db, "SELECT " + ",".join(f"g.{name} AS {name}" for name in _LOAD_COLUMNS) + " FROM guidance_loads g "
        "JOIN (SELECT json_extract(value,'$[0]') AS thread_id,json_extract(value,'$[1]') AS turn_id "
        "FROM json_each(?)) w ON g.thread_id=w.thread_id AND g.turn_id=w.turn_id",
        (wanted,), _LOAD_COLUMNS, "created_at,id",
    ):
        grouped[(str(load["thread_id"]), str(load["turn_id"]), str(load["path"]))].append(load)
    return grouped


def read_bodies(
    db, *, user_id: str, thread_ids: tuple[str, ...], facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Batch scoped potential reads before loading bodies or binding predecessors."""
    unavailable: Counter[str] = Counter()
    candidates = _candidate_groups(db, user_id, thread_ids, unavailable)
    loads_by_group = _loads_by_group(db, candidates)

    surviving: list[tuple[Any, dict[str, Any], Any]] = []
    for key, calls in candidates.items():
        loads = loads_by_group[key]
        lesson_receipts = [
            load for load in loads
            if load["lesson_id"] is not None and load["lesson_behavioral_sha256"] is not None
        ]
        unambiguous_lessons = len({
            (load["lesson_id"], load["lesson_behavioral_sha256"])
            for load in lesson_receipts
        }) == len(lesson_receipts)
        if (
            len(calls) != 1 or not loads
            or (len(loads) > 1 and (len(lesson_receipts) != len(loads) or not unambiguous_lessons))
        ):
            unavailable["repeated or missing call/load linkage"] += len(calls)
            continue
        row, metadata = calls[0]
        surviving.extend((row, metadata, load) for load in loads)

    bodies: dict[str, Any] = {}
    wanted = json.dumps([list(pair) for pair in dict.fromkeys(
        (str(row["thread_id"]), str(row["id"])) for row, _metadata, _load in surviving)])
    for row in json_row_chunks(
        db, "SELECT m.id AS id,m.thread_id AS thread_id,m.metadata AS metadata,m.content AS content "
        "FROM messages m JOIN (SELECT json_extract(value,'$[0]') AS thread_id,json_extract(value,'$[1]') AS id "
        "FROM json_each(?)) w ON m.thread_id=w.thread_id AND m.id=w.id WHERE m.role='tool'",
        (wanted,), ("id", "thread_id", "metadata", ("content", "hex(content)")), "id",
    ) if surviving else ():
        # Content travels as hex so embedded NUL bytes survive SQLite's JSON text functions.
        row["content"] = bytes.fromhex(row["content"]).decode() if row["content"] else None
        bodies[str(row["id"])] = row

    rows = [bodies[str(row["id"])] for row, _metadata, _load in surviving if str(row["id"]) in bodies]
    canonical = canonical_result_reasons(db, rows)
    reads: list[dict[str, Any]] = []
    for row, metadata, load in surviving:
        body = bodies.get(str(row["id"]))
        if body is None:
            unavailable["canonical invocation lineage unavailable"] += 1
            continue
        reason = canonical[str(body["id"])]
        if reason is not None:
            unavailable[reason] += 1
            continue
        record, reason = _joined_read(db, body, metadata, load, facts)
        if record is not None:
            reads.append(record)
        else:
            unavailable[str(reason)] += 1
    return {"reads": reads, "unavailable_reads": dict(unavailable),
            "coverage": (
                "canonical read_file only; shell reads and unrecorded lineage unavailable; "
                "native results before the native-writer fix lack invocation lineage and are "
                "counted as unavailable rather than reads; predecessor Turn lineage refuses "
                "provider-call-id reuse across Turns"
            )}
