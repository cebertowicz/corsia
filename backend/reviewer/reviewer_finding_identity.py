"""Stable identity for one review finding.

A finding's identity decides whether a later review round is looking at the
same problem or a new one. Getting a finding onto the PR belongs to
``reviewer.review_publication``; classifying and posting threads belongs to
``reviewer.reviewer_threads``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from events.markers import REVIEW_FP_MARKER_PREFIX
from reviewer.reviewer_categories import (
    SEVERITY_BLOCK,
    SEVERITY_FLAG,
    SEVERITY_NIT,
)

_MARKER_RE = re.compile(REVIEW_FP_MARKER_PREFIX + r"\s*([0-9a-f]{16})\s*-->")
_MARKER_FMT = REVIEW_FP_MARKER_PREFIX + " {fp} -->"


def fingerprint_finding(finding: dict[str, Any]) -> str:
    """Compute a finding identity that survives the model rewording itself.

    Identity must not depend on the finding's prose. A reviewer paraphrases
    the same finding on every run, so hashing its description gave one issue
    a fresh identity each round: it opened a new thread, counted as new, and
    no author action could settle it. Measured on PR #1190, where a single
    ``architecture/loc-budget`` finding about one file produced eight threads
    under eight fingerprints across eight rounds.

    A cited standards or PRD obligation is a stable identifier the model does
    not invent, so it separates findings when present. Otherwise identity is
    the file and category alone, so two distinct same-category findings in one
    file share an identity. ``_group_by_fingerprint`` merges such a collision
    into one thread carrying every description rather than discarding any, so
    the cost is a combined thread and never a lost finding. Issue #1249 removes
    the need for a content hash altogether by letting the reviewer read its own
    prior threads.
    """
    file_path = (finding.get("file") or "").strip()
    category = (finding.get("category") or "").strip()
    cited = (finding.get("cited_section") or "").strip().lower()

    payload = f"{file_path}|{category}|{cited}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


_SEVERITY_RANK = {SEVERITY_NIT: 0, SEVERITY_FLAG: 1, SEVERITY_BLOCK: 2}


def _group_by_fingerprint(
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Map each fingerprint to one finding, merging collisions losslessly.

    Identity deliberately excludes the model's prose so a rephrased finding
    dedups across rounds. The cost is that two distinct findings sharing a
    file, category, and cited obligation collide within a single round.

    Keying a dict on that would keep only the last: just the survivor reaches
    ``new_threads``, and ``review_publication`` reconciles what it delivered
    against that already-collapsed count -- so the loss is invisible to the
    very safety net that module exists to provide. Severity is not part of
    identity either, so a ``block`` could be discarded by a later ``flag`` and
    never reach the PR at all. That is the issue #946 failure mode.

    A collision therefore merges instead of overwriting: the group keeps the
    highest severity present and carries every description into one thread.
    Nothing the reviewer produced is lost.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        grouped.setdefault(fingerprint_finding(finding), []).append(finding)
    return {fp: _merge_findings(group) for fp, group in grouped.items()}


def _severity_rank(finding: dict[str, Any]) -> int:
    """Order severities so a merged group can keep the strongest one."""
    return _SEVERITY_RANK.get(str(finding.get("severity") or "").lower(), 0)


def _merge_findings(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse findings sharing one fingerprint into a single finding."""
    if len(group) == 1:
        return group[0]

    merged = dict(max(group, key=_severity_rank))
    parts = []
    for finding in group:
        severity = str(finding.get("severity") or "nit").upper()
        line = finding.get("line")
        where = " (line %s)" % line if line else ""
        text = finding.get("description") or "(no description)"
        parts.append("- **%s**%s: %s" % (severity, where, text))
    merged["description"] = "\n".join(
        ["%d findings share this file and category:" % len(group), ""] + parts
    )
    return merged


def extract_fingerprint(body: str | None) -> str | None:
    """Return the fingerprint marker from a comment body."""
    if not body:
        return None
    match = _MARKER_RE.search(body)
    return match.group(1) if match else None


