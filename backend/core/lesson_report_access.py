"""Resolve existing caller visibility and sources for both lesson-report adapters."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from threading import Event
from typing import Any

from core.lesson_corrections import correction_facts, source_subjects
from core.lesson_report import ReportScope, collect_report, detail_request, read_report
from core.lesson_report_db import report_snapshot
from core.lesson_report_offers import LessonReportCollectionCancelled
from core.lesson_report_sources import current_sources, source_inputs
from core.measurement_scope import completed_week_window, scope_cte, validate_scope
from infra.db_async import run_bulk, run_off_db
from infra.guidance_git import recorded_blob_reader


def resolve_scope(db, *, user_id: str, caller_thread_id: str | None, view: str,
                  project: str, agent: str | None, period: str) -> ReportScope:
    """Filters never grant access; the existing same-user closure owns membership."""
    if view not in {"self", "all", "project", "agent"}:
        raise ValueError("scope must be self, all, project, or agent")
    if view in {"self", "all"} and (project != "all" or agent not in {None, "none"}):
        raise ValueError("self/all scope does not accept a project or agent filter")
    if view == "project" and agent not in {None, "none"}:
        raise ValueError("choose scope=agent for an agent filter")
    if view == "self":
        row = db.execute("SELECT user_id FROM threads WHERE id=?", (caller_thread_id,)).fetchone()
        if row is None or row["user_id"] != user_id:
            raise PermissionError("caller thread unavailable for this identity")
        ids = (str(caller_thread_id),)
        identity = f"self:{caller_thread_id}"
    else:
        if view == "project" and project == "all":
            raise ValueError('project scope needs project=<id>; use scope="all" for all work')
        if view == "agent" and not agent:
            raise ValueError("agent scope needs agent=<id>")
        selected = validate_scope(db, user_id, project if view != "all" else "all", agent)
        ids = tuple(row["id"] for row in db.execute(
            scope_cte() + "SELECT id FROM scope_threads ORDER BY id", selected.params(user_id),
        ))
        identity = f"{view}:{selected.project}:{selected.agent or 'none'}"
    start, end = completed_week_window(period, date.min)
    return ReportScope(user_id, ids, identity, start, end, True)


# File discovery is separate from database collection so it cannot pin the WAL.


@dataclass(frozen=True)
class ReportCollection:
    scope: ReportScope
    inputs: list[dict[str, Any]]
    collected: dict[str, Any]
    options: dict[str, Any]
    range: str
    view: str
    project: str
    agent: str | None


def collect_for_identity(*, user_id: str | None = None, workspace: Path, caller_thread_id: str | None = None,
                        scope: str = "self", project: str = "all", agent: str | None = None,
                        range: str = "12w", cancellation_event: Event | None = None,
                        **options: Any) -> ReportCollection:
    """Collect database-only facts for a later immutable render."""
    snapshot = (report_snapshot(cancellation_event=cancellation_event)
                if cancellation_event is not None else report_snapshot())
    with snapshot as db:
        if user_id is None:
            caller = db.execute("SELECT user_id FROM threads WHERE id=?", (caller_thread_id,)).fetchone()
            if caller is None:
                raise PermissionError("caller identity unavailable")
            user_id = caller["user_id"]
        selected = resolve_scope(db, user_id=user_id, caller_thread_id=caller_thread_id,
                                 view=scope, project=project, agent=agent, period=range)
        inputs = source_inputs(db, workspace, selected)
        detail = detail_request(options.get("lesson_id"), options.get("version"), options.get("cursor"),
                                options.get("evidence"))
        collected = collect_report(db, scope=selected, sources=[], detail=detail,
                                   cancellation_event=cancellation_event)
        if cancellation_event is not None and cancellation_event.is_set():
            raise LessonReportCollectionCancelled
    return ReportCollection(selected, inputs, collected, dict(options), range, scope, project, agent)


def collect_corrections(collection: ReportCollection, sources: list[dict[str, Any]],
                        cancellation_event: Event | None = None) -> ReportCollection:
    """Join correction facts for current lessons' subjects in a second short snapshot.

    Subjects live in lesson files, which are read only after the first snapshot closes.
    """
    subjects = source_subjects(sources)
    if not subjects:
        return collection
    snapshot = (report_snapshot(cancellation_event=cancellation_event)
                if cancellation_event is not None else report_snapshot())
    with snapshot as db:
        facts = correction_facts(db, subjects)
    return replace(collection, collected={**collection.collected, "correction_facts": facts})


def render_collection(collection: ReportCollection, workspace: Path,
                      resolved: tuple[list, list, dict] | None = None) -> dict[str, Any]:
    """Resolve filesystem and Git evidence only after the DB snapshot is closed."""
    sources, unavailable, discovery = resolved or current_sources(workspace, collection.inputs)
    blob_reader, blob_reason = recorded_blob_reader(workspace)
    try:
        result = read_report(None, scope=collection.scope, sources=sources, collected=collection.collected,
                             source_coverage={"unavailable_sources": unavailable, "source_discovery": discovery,
                                              "guidance_history": blob_reason},
                             blob_reader=blob_reader,
                             now=datetime.now(timezone.utc).isoformat(), **collection.options)
    finally:
        if blob_reader is not None:
            blob_reader.close()
    result["range"] = collection.range
    filters = {"scope": collection.view, "project": collection.project,
               "agent": collection.agent or "none", "range": collection.range}
    filters = {key: value for key, value in filters.items() if value is not None}

    def call(**detail):
        args = {**filters, **detail}
        return "lesson_report(" + ", ".join(f"{key}={json.dumps(value)}" for key, value in args.items()) + ")"

    for row in result.get("rows", []):
        if "next_call" in row:
            row["next_call"] = call(lesson_id=row["id"], version=row["version"])
    if result.get("detail"):
        row = result["detail"]
        row["next_call"] = call(lesson_id=row["id"], version=row["version"])
    elif "detail" in result:
        result["next_call"] = call(scope="all", project="all", agent="none",
                                   lesson_id=collection.options.get("lesson_id"))
    if result.get("unidentified_next_call"):
        result["unidentified_next_call"] = call(evidence="unidentified_reads")
    return result


async def report_for_identity(*, cancellation_event: Event | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run the read-only snapshot and immutable render phases on their owners."""
    workspace = kwargs["workspace"]
    collection = await run_bulk(collect_for_identity, cancellation_event=cancellation_event, **kwargs)
    resolved = await run_off_db(current_sources, workspace, collection.inputs)
    collection = await run_bulk(collect_corrections, collection, resolved[0], cancellation_event)
    return await run_off_db(render_collection, collection, workspace, resolved)
