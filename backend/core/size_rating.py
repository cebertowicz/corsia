"""Estimated outcome size: the one owner of the rating fact and its identity.

A size rating is a typed ``outcome_events`` fact about one accepted plan node.
It never creates an outcome: the canonical Measurement population decides which
nodes count, in which accepted week and scope. This module owns the fact's name,
its attributes, the fixed class midpoints, and the source snapshot a rating was
made from, so the importer and the served series cannot disagree about any of them.

The midpoints are fixed rubric values, not measured human time and not per-user
policy. The classes describe human-equivalent effort for the delivered result:
XS is under 15 minutes, S under 1 hour, M under 4 hours, L under 16 hours, XL
under 80 hours, and XXL more; ``unknown`` is an explicit declaration that it
cannot be sized and contributes no hours. A missing size is not recorded, and
is distinct from ``unknown``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SIZE_EVENT = "measurement.size_rating"
SUBJECT_PREFIX = "plan_node:"
NO_EVIDENCE = "none"
BASIS_DELIVERED = "delivered_evidence_proxy"
BASIS_ACCEPTED = "accepted_scope_proxy"
# Imported facts remain independent ratings; author declarations appear only in
# the served measurement mix and are never valid imported-fact bases.
BASIS_AUTHOR_DECLARED = "author_declared"
BASES = (BASIS_DELIVERED, BASIS_ACCEPTED)
SERVED_BASES = (*BASES, BASIS_AUTHOR_DECLARED)
UNKNOWN = "unknown"
UNKNOWN_VERSION = "unknown"
CLASS_MIDPOINT_HOURS: dict[str, float] = {
    "XS": 0.1, "S": 0.5, "M": 2.0, "L": 8.0, "XL": 40.0, "XXL": 120.0,
}
SIZE_CLASSES = (*CLASS_MIDPOINT_HOURS, UNKNOWN)
# The full meaning goes where it costs nothing per Turn (skill, refusal); the
# published tool field carries only a short prefix and the scale.
SIZE_MEANING = (
    "How long a capable person would need to deliver this result by hand, including the reading, "
    "thinking, drafting, checking and revising it takes; not how long you will take."
)
SIZE_TOOL_PREFIX = "Effort for a capable person, not your time: "
SIZE_SCALE_DESCRIPTION = (
    "XS under 15 min, S under 1 h, M under 4 h, L under 16 h, XL under 80 h, "
    "XXL more; unknown if you cannot tell"
)
SHA256_KEYS = ("source_snapshot_sha256", "rater_input_sha256", "rubric_sha256")
# Every attribute a rating must carry; a fact missing one is malformed, not partial.
FACT_KEYS = (
    "node_id", "plan_id", "node_revision", "evidence_event_id",
    "source_snapshot_sha256", "rater_input_sha256", "method", "method_version",
    "rubric_version", "rubric_sha256", "rater", "rater_version", "basis", "size_class",
)
PROFILE_KEYS = ("method", "method_version", "rubric_version", "rubric_sha256", "rater", "rater_version")
# A declared size has no rater or rubric; it is its own profile so a week or range that
# mixes it with imported ratings is not served as comparable.
AUTHOR_DECLARED_PROFILE = (BASIS_AUTHOR_DECLARED, *("none",) * (len(PROFILE_KEYS) - 1))

# The latest delivered evidence for a node: a valid JSON plan event whose
# ``$.evidence`` is non-empty text, newest first. Its id is the identity a rating
# was made against; the same row supplies the evidence text the snapshot hashes.
LATEST_EVIDENCE_SQL = """
    SELECT e.{column} FROM plan_node_events e
    WHERE e.node_id={node} AND json_valid(e.payload_json)
      AND json_type(e.payload_json,'$.evidence')='text'
      AND length(json_extract(e.payload_json,'$.evidence'))>0
    ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1
"""


def subject_key(node_id: str) -> str:
    """The ``outcome_events`` subject a node's ratings are filed and looked up under."""
    return SUBJECT_PREFIX + node_id


def compose_request(title: str | None, description: str | None, criteria: str | None) -> str:
    """Compose the accepted request text exactly as the rating export composed it."""
    parts = []
    if (title or "").strip():
        parts.append("Title: " + (title or "").strip())
    if (description or "").strip():
        parts.append("Description: " + (description or "").strip())
    if (criteria or "").strip():
        parts.append("Acceptance criteria: " + (criteria or "").strip())
    return "\n".join(parts)


@dataclass(frozen=True)
class SourceSnapshot:
    """The current identity and content one rating must have been made from."""

    node_id: str
    plan_id: str
    node_revision: int
    evidence_event_id: str
    basis: str
    request: str
    evidence: str

    def sha256(self) -> str:
        """SHA-256 of the compact, key-sorted UTF-8 JSON of exactly these seven fields."""
        payload = json.dumps(
            {"node_id": self.node_id, "plan_id": self.plan_id, "node_revision": self.node_revision,
             "evidence_event_id": self.evidence_event_id, "basis": self.basis,
             "request": self.request, "evidence": self.evidence},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_text(payload_json: str | None) -> str:
    try:
        payload = json.loads(payload_json) if payload_json else None
    except (TypeError, ValueError):
        return ""
    value = payload.get("evidence") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else ""


def current_snapshots(db, node_ids: list[str]) -> dict[str, SourceSnapshot]:
    """Read the current source snapshot for each existing node id."""
    result: dict[str, SourceSnapshot] = {}
    sql = (
        "SELECT n.id,n.plan_id,n.revision,n.title,n.description,n.acceptance_criteria,"
        f"({LATEST_EVIDENCE_SQL.format(column='id', node='n.id')}),"
        f"({LATEST_EVIDENCE_SQL.format(column='payload_json', node='n.id')}) "
        "FROM plan_nodes n WHERE n.id=?"
    )
    for node_id in node_ids:
        row = db.execute(sql, (node_id,)).fetchone()
        if row is None:
            continue
        evidence = _evidence_text(row[7])
        result[node_id] = SourceSnapshot(
            node_id=row[0], plan_id=row[1], node_revision=int(row[2]),
            evidence_event_id=row[6] or NO_EVIDENCE,
            basis=BASIS_DELIVERED if evidence.strip() else BASIS_ACCEPTED,
            request=compose_request(row[3], row[4], row[5]), evidence=evidence,
        )
    return result


def profile(attributes: dict[str, Any]) -> tuple[str, ...]:
    """The rating profile: who rated, with which method and rubric version."""
    return tuple(str(attributes.get(key)) for key in PROFILE_KEYS)


def fact_status(attributes: Any, retracted: bool, current: tuple[str, int, str]) -> str:
    """Classify one newest fact against its node's current identity.

    ``current`` is (plan id, revision, latest evidence-event id or ``none``).
    Only ``usable`` contributes hours; every other status is counted, never
    replaced by an older rating.
    """
    if retracted:
        return "retracted"
    if not isinstance(attributes, dict) or any(key not in attributes for key in FACT_KEYS):
        return "malformed"
    if attributes["size_class"] not in SIZE_CLASSES or attributes["basis"] not in BASES:
        return "malformed"
    plan_id, revision, evidence_event_id = current
    if (attributes["plan_id"] != plan_id or attributes["node_revision"] != revision
            or attributes["evidence_event_id"] != evidence_event_id):
        return "stale"
    return "unknown" if attributes["size_class"] == UNKNOWN else "usable"
