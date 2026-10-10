"""Parse LLM reviewer output into validated findings."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from reviewer.reviewer_categories import (
    APPROACH_CHURN_CATEGORY,
    SEVERITY_BLOCK,
    VALID_CATEGORIES_SET,
    VALID_SEVERITIES,
)

log = logging.getLogger(__name__)


class ReviewerOutputError(ValueError):
    """Reviewer output could not be parsed as a JSON findings array."""


def parse_findings(raw_output: str) -> list[dict[str, Any]]:
    """Parse the reviewer's raw output into a list of validated findings."""
    text = raw_output.strip()
    if not text:
        raise ReviewerOutputError(
            "reviewer returned empty output (a clean review is the literal [])"
        )

    text = _strip_code_fences(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _recover_json_array(text)
        if data is None:
            raise ReviewerOutputError(
                f"reviewer output is not valid JSON: {text[:200]}"
            )

    if not isinstance(data, list):
        raise ReviewerOutputError(
            f"reviewer output is not a JSON array: {type(data).__name__}"
        )

    findings: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            log.warning("Finding #%d is not a dict, skipping: %s", i, type(item).__name__)
            continue
        finding = _validate_finding(item, index=i)
        findings.append(finding)

    return findings


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences wrapping the JSON."""
    pattern = r"^```(?:json)?\s*\n?(.*?)\n?\s*```$"
    match = re.match(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _recover_json_array(text: str) -> list[dict[str, Any]] | None:
    """Try to extract a JSON array from text that has extra content."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _validate_finding(item: dict[str, Any], index: int) -> dict[str, Any]:
    """Validate and normalize a single finding dict."""
    severity = item.get("severity", "nit")
    if severity not in VALID_SEVERITIES:
        log.warning("Finding #%d: invalid severity %r, defaulting to 'nit'", index, severity)
        severity = "nit"

    category = item.get("category", "")
    if category not in VALID_CATEGORIES_SET:
        log.warning("Finding #%d: unknown category %r", index, category)
    if category == APPROACH_CHURN_CATEGORY and severity != SEVERITY_BLOCK:
        log.warning(
            "Finding #%d: approach churn must block; coercing severity %r",
            index, severity,
        )
        severity = SEVERITY_BLOCK

    file_path = item.get("file", "unknown")
    line = item.get("line", 0)
    if not isinstance(line, int):
        try:
            line = int(line)
        except (ValueError, TypeError):
            line = 0

    description = item.get("description", "(no description)")
    cited_section = item.get("cited_section")

    return {
        "severity": severity,
        "file": file_path,
        "line": line,
        "category": category,
        "description": description,
        "cited_section": cited_section,
    }
