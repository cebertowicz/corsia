"""Finding schema for anti-pattern detector results.

A Finding is what a rule emits when it matches. The engine collects findings,
filters by severity, and renders them in human or JSON format.

Severity values (string, not enum, to keep JSON serialization trivial):
    "error"   — pre-commit gate blocks the commit.
    "warning" — pre-commit gate reports without blocking.
    "info"    — informational only; never blocks.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Iterable


SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_VALID_SEVERITIES = {SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO}

# Rank used when sorting findings (errors first) and when filtering by
# `--severity` threshold (a `--severity warning` run still surfaces errors).
_SEVERITY_RANK = {
    SEVERITY_ERROR: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_INFO: 2,
}


@dataclass
class Finding:
    """A single anti-pattern detector hit.

    Fields:
        rule_id: Stable identifier of the rule that produced this finding.
        severity: One of "error" | "warning" | "info".
        file: Repo-relative path of the offending file.
        line: 1-indexed line number of the offending source.
        col: 1-indexed column, optional (None when not available).
        snippet: A short slice of the offending source for terminal context.
        message: Human-readable explanation of why this fired.
        fix_hint: Optional suggested edit, displayed in human output.
    """

    rule_id: str
    severity: str
    file: str
    line: int
    col: int | None
    snippet: str
    message: str
    fix_hint: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate severity at construction so authoring errors fail loud."""
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Finding.severity must be one of {sorted(_VALID_SEVERITIES)}; "
                f"got {self.severity!r} for rule {self.rule_id!r}."
            )

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict (matches `Finding` schema)."""
        return asdict(self)


def severity_at_or_above(threshold: str) -> set[str]:
    """Return the set of severities at or stricter than `threshold`.

    severity_at_or_above("warning") -> {"error", "warning"}
    severity_at_or_above("error")   -> {"error"}
    """
    if threshold not in _VALID_SEVERITIES:
        raise ValueError(f"Unknown severity threshold {threshold!r}.")
    cutoff = _SEVERITY_RANK[threshold]
    return {s for s, rank in _SEVERITY_RANK.items() if rank <= cutoff}


def findings_to_json(findings: Iterable[Finding]) -> str:
    """Serialize an iterable of Findings to a stable JSON string.

    The top-level shape is `{"findings": [...]}`. Stable order: by file,
    then line, then column, then rule_id — so diff-driven tooling sees a
    deterministic layout across runs.
    """
    sorted_findings = sorted(
        findings,
        key=lambda f: (f.file, f.line, f.col or 0, f.rule_id),
    )
    return json.dumps(
        {"findings": [f.to_dict() for f in sorted_findings]},
        indent=2,
        sort_keys=False,
    )


def render_human(findings: Iterable[Finding]) -> str:
    """Render findings as a human-readable terminal block.

    One line per finding header, indented snippet + fix hint below.
    """
    sorted_findings = sorted(
        findings,
        key=lambda f: (
            _SEVERITY_RANK[f.severity],
            f.file,
            f.line,
            f.col or 0,
            f.rule_id,
        ),
    )
    if not sorted_findings:
        return "antipatterns: no findings."
    lines: list[str] = []
    for f in sorted_findings:
        head = f"{f.severity.upper():<7} {f.rule_id:<28} {f.file}:{f.line}"
        if f.col is not None:
            head += f":{f.col}"
        lines.append(head)
        lines.append(f"  {f.message}")
        if f.snippet:
            snippet_preview = f.snippet.strip().splitlines()[0][:120]
            lines.append(f"    > {snippet_preview}")
        if f.fix_hint:
            lines.append(f"  fix: {f.fix_hint}")
        lines.append("")
    error_count = sum(1 for f in sorted_findings if f.severity == SEVERITY_ERROR)
    warning_count = sum(1 for f in sorted_findings if f.severity == SEVERITY_WARNING)
    info_count = sum(1 for f in sorted_findings if f.severity == SEVERITY_INFO)
    lines.append(
        f"antipatterns: {error_count} error(s), {warning_count} warning(s), "
        f"{info_count} info."
    )
    return "\n".join(lines)
