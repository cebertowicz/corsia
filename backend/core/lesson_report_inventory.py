"""Known lesson/version inventory; review nodes and offers are not trial counts.

Inputs are facts already visible to the caller. This projection does not resolve
paths, authorize owner-wide history, or interpret free-text review dispositions.
"""
from __future__ import annotations

from typing import Any

from core.lesson_files import raw_sha256, read_capture, read_trial
from core.lesson_trials import parse_instant
from core.skill_validation import validate_skill_text

Identity = tuple[str, str, str]

# The canonical trial body is parsed by core.lesson_files; this inventory only projects it.

def source_record(text: str, path: str, *, with_statement: bool = False) -> dict[str, Any] | None:
    """Project one authorized current source using the canonical file validators.

    ``with_statement`` adds the trial's one-line statement for list rows; recorded
    reads keep their original shape.
    """
    trial = read_trial(text)
    if trial:
        lesson = trial.lesson
        # The canonical Lesson normalizes dates to instants for scheduling.
        # Display preserves the validated source's calendar-date precision.
        block = validate_skill_text(text).metadata["corsia"]["lesson"]
        record = {
            "kind": "lesson", "id": lesson.id, "version": trial.behavioral_sha256,
            "name": trial.name, "state": lesson.state, "owner_id": lesson.owner,
            "review_after": block["review_after"], "review_node_id": lesson.review_node_id,
            "path": path, "raw_sha256": raw_sha256(text.encode()),
            "source_refs": list(lesson.source_refs),
        }
        if with_statement:
            if trial.statement is not None:
                record["statement"] = trial.statement
            elif trial.statement_error is not None:
                record["statement_error"] = trial.statement_error
        if "originating_subjects" in block:
            record["originating_subjects"] = list(lesson.originating_subjects)
        return record
    capture = read_capture(text)
    if capture:
        from core.skill_validation import parse_frontmatter

        metadata, _ = parse_frontmatter(text)
        block = metadata["corsia"]["capture"]
        record = {
            "kind": "capture", "id": capture.id, "version": capture.hash,
            "name": capture.id, "state": block.get("state"), "path": path,
            "owner_id": block.get("owner"), "review_after": block.get("review_after"),
            "raw_sha256": raw_sha256(text.encode()), "source_refs": list(capture.source_refs),
        }
        if "originating_subjects" in block:
            record["originating_subjects"] = list(capture.originating_subjects)
        return record
    return None


def _identity(fact: dict[str, Any]) -> Identity | None:
    kind, subject, version = fact.get("kind"), fact.get("id"), fact.get("version")
    if kind not in {"lesson", "capture"} or not isinstance(subject, str) or not subject:
        return None
    if not isinstance(version, str) or not version:
        return None
    return kind, subject, version


def _new(identity: Identity) -> dict[str, Any]:
    kind, subject, version = identity
    return {
        "kind": kind, "id": subject, "version": version, "name": subject,
        "activated": None if kind == "lesson" else False, "state": None, "current_source": None,
        "source_status": "unavailable", "owner_ids": [], "reviews": [],
        "retained": [], "due": None, "review_after": None,
        "lifecycle_reason": "current source unavailable",
        "extension_history_complete": False, "deleted_history_complete": False,
    }


def _review_work(row: dict[str, Any], now: str) -> None:
    """An open review is an obligation, independent of known source lifecycle."""
    instant = parse_instant(now)
    due_nodes: list[str] = []
    unknown = 0
    for review in row["reviews"]:
        if not review["open"]:
            continue
        when = parse_instant(review.get("scheduled_for"))
        if when is None or instant is None:
            unknown += 1
        elif when <= instant:
            due_nodes.append(review["node_id"])
    review_due = True if due_nodes else None if unknown else False
    row.update({"open_reviews": sum(bool(review["open"]) for review in row["reviews"]),
                "review_due": review_due, "due_review_nodes": due_nodes,
                "review_due_unknown": unknown})
    known_due = (row["due"], review_due)
    row["due_work"] = True if True in known_due else None if None in known_due else False


def build_inventory(
    *, history: list[dict[str, Any]], sources: list[dict[str, Any]],
    retained: list[dict[str, Any]], now: str,
) -> list[dict[str, Any]]:
    """Retain every known exact identity, including absent/changed current files.

    A review proves a review was created, not that its activation edit succeeded.
    A captured review never becomes an
    activated trial. An offer proves that version was offered but not consumed.
    Administrative variants sharing a behavior hash remain separate source facts.
    """
    rows: dict[Identity, dict[str, Any]] = {}
    for review in history:
        identity = _identity({"kind": review.get("kind"), "id": review.get("subject_id"),
                              "version": review.get("subject_hash")})
        if identity is None:
            continue
        row = rows.setdefault(identity, _new(identity))
        if all(existing["node_id"] != review["node_id"] for existing in row["reviews"]):
            row["reviews"].append(dict(review))
        owner = review.get("owner_id")
        if owner and owner not in row["owner_ids"]:
            row["owner_ids"].append(owner)
    for fact in retained:
        identity = _identity(fact)
        if identity is None:
            continue
        row = rows.setdefault(identity, _new(identity))
        row["retained"].append(dict(fact))
        if row["kind"] == "lesson" and fact.get("offered") is True:
            row["activated"] = True
    current: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for source in sources:
        identity = _identity(source)
        if identity is None:
            continue
        row = rows.setdefault(identity, _new(identity))
        current.setdefault(identity[:2], []).append(dict(source))
        owner = source.get("owner_id")
        if owner and owner not in row["owner_ids"]:
            row["owner_ids"].append(owner)
        if source.get("review_node_id"):
            row["activated"] = row["kind"] == "lesson"
    for identity, row in rows.items():
        candidates = current.get(identity[:2], [])
        matching = [source for source in candidates if source["version"] == identity[2]]
        if matching:
            row["source_status"] = "current" if len(matching) == 1 else "ambiguous"
            row["current_source"] = matching[0] if len(matching) == 1 else None
            row["name"] = matching[0]["name"]
            if len(matching) == 1:
                source = matching[0]
                row["state"] = source["state"]
                row["review_after"] = source.get("review_after")
                if "originating_subjects" in source:
                    row["originating_subjects"] = list(source["originating_subjects"])
                row["lifecycle_reason"] = None
                row["due"] = False if row["state"] != "trial" else None
                when, instant = parse_instant(row["review_after"]), parse_instant(now)
                if row["kind"] == "lesson" and row["state"] == "trial" and when and instant:
                    row["due"] = when <= instant
            else:
                row["lifecycle_reason"] = "multiple current sources for this behavioral version"
        elif candidates:
            row["source_status"] = "changed"
            row["lifecycle_reason"] = "current source has a different behavioral version"
        _review_work(row, now)
    return sorted(rows.values(), key=lambda row: (
        row["due_work"] is not True, row["review_after"] or "9999",
        tuple(sorted(row["owner_ids"])), row["kind"], row["id"], row["version"],
    ))
