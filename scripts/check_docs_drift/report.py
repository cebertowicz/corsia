"""Text and JSON rendering for the docs-drift check results."""

from __future__ import annotations

import json
from dataclasses import asdict

from .citation_check import CitationResult
from .private_scan_check import PrivateScanResult


def overall_ok(ci: CitationResult, ps: PrivateScanResult) -> bool:
    return ci.ok and ps.ok


def _verdict(ci: CitationResult, ps: PrivateScanResult) -> str:
    """The report's last line. An accepted skip is never rendered as a plain PASS.

    A run whose owner-identity (or internal-name) sub-check never executed has
    not seen the whole set of public files for that class of leak, so the
    verdict names what did not run rather than reporting the run as complete
    (PS7: what the reader sees is the truth). The same applies per kind: a run
    that resolved only contact literals never checked for the owner's name.
    """
    if not overall_ok(ci, ps):
        return "FAIL"
    person_gap = None
    if ps.owner_identity_kinds and "person" not in ps.owner_identity_kinds:
        person_gap = "owner-name sub-check NOT run: " + ps.owner_identity_kind_reasons.get(
            "person", "no person-name token was resolved"
        )
    skipped = [
        f"owner-identity sub-check NOT run: {ps.owner_identity_skipped_reason}"
        if ps.owner_identity_skipped_reason
        else person_gap,
        "internal-name sub-check NOT run" if ps.internal_names_skipped_reason else None,
    ]
    caveats = [item for item in skipped if item]
    return "PASS (" + "; ".join(caveats) + ")" if caveats else "PASS"


def to_json(ci: CitationResult, ps: PrivateScanResult) -> str:
    payload = {
        "ok": overall_ok(ci, ps),
        "verdict": _verdict(ci, ps),
        "citations": asdict(ci),
        "private_names": asdict(ps),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def to_text(ci: CitationResult, ps: PrivateScanResult) -> str:
    lines = ["# Documentation drift report", ""]
    lines += _citations_section(ci)
    lines += _private_scan_section(ps)
    lines.append(_verdict(ci, ps))
    return "\n".join(lines)


def _citations_section(r: CitationResult) -> list[str]:
    lines = [
        "## 1. Cited commands, routes, paths, and versions",
        "",
        f"checked {r.checked_paths} paths, {r.checked_routes} routes, "
        f"{r.checked_subcommands} corsia subcommands",
    ]
    for path in r.missing_paths:
        lines.append(f"MISSING PATH: {path}")
    for route in r.missing_routes:
        lines.append(f"MISSING ROUTE: {route}")
    for cmd in r.missing_subcommands:
        lines.append(f"UNHANDLED SUBCOMMAND: {cmd}")
    for mismatch in r.python_version_mismatches:
        lines.append(f"VERSION MISMATCH: {mismatch}")
    return lines + [""]


def _private_scan_section(r: PrivateScanResult) -> list[str]:
    lines = ["## 2. Private names and paths in public files", ""]
    if r.owner_identity_skipped_reason:
        lines.append(f"owner-identity sub-check SKIPPED: {r.owner_identity_skipped_reason}")
    elif r.owner_identity_kinds:
        lines.append(f"owner-identity kinds scanned: {'+'.join(r.owner_identity_kinds)}")
    for kind, reason in sorted(r.owner_identity_kind_reasons.items()):
        lines.append(f"owner-identity kind NOT scanned: {kind}: {reason}")
    if r.internal_names_skipped_reason:
        lines.append(f"internal-name sub-check SKIPPED: {r.internal_names_skipped_reason}")
    for path, hits in sorted(r.home_dir_hits.items()):
        lines.append(f"HOME DIR: {path}: {hits}")
    for path, hits in sorted(r.owner_hits.items()):
        lines.append(f"OWNER IDENTITY: {path}: {hits}")
    for path, hits in sorted(r.internal_name_hits.items()):
        lines.append(f"INTERNAL NAME: {path}: {hits}")
    return lines + [""]
