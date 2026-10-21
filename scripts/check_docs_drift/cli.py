"""Argument parsing and process exit-code wiring for check-docs-drift."""

from __future__ import annotations

import argparse
from pathlib import Path

from .citation_check import check_citations
from .discovery import repo_root
from .owner_identity import OwnerProfileTokens, read_owner_profile
from .private_scan_check import check_private_names, read_token_file
from .report import overall_ok, to_json, to_text


def build_parser() -> argparse.ArgumentParser:
    """Build the ``check-docs-drift`` argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Check for drift between published docs and their sources: "
            "cited commands/routes/paths/versions and private names/paths "
            "in publicly-shipped files."
        )
    )
    parser.add_argument(
        "--internal-names-file",
        default=None,
        type=Path,
        help="Optional file of internal agent/lane/thread names to scan public docs for",
    )
    parser.add_argument(
        "--owner-name",
        default=None,
        help="Owner identity token(s) to scan for, overriding git config user.name/user.email",
    )
    parser.add_argument(
        "--owner-identity-file",
        default=None,
        type=Path,
        help=(
            "File of owner identity tokens (name, email), one per line, kept outside "
            "this public repository; overrides git config, which may hold a bot identity"
        ),
    )
    parser.add_argument(
        "--owner-profile",
        default=None,
        type=Path,
        help="Workspace USER.md profile; overridden by --owner-name and --owner-identity-file",
    )
    parser.add_argument(
        "--allow-missing-owner-identity",
        action="store_true",
        help=(
            "Treat an unresolved owner identity as an accepted skip instead of a "
            "failure -- for a checkout with none configured (a plain clone, CI); "
            "the report names the skip and the verdict is not a plain PASS"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of a text report"
    )
    parser.add_argument(
        "--json-out", type=Path, help="Write the machine-readable report to this path while stdout stays text"
    )
    return parser


def main(argv: list[str]) -> int:
    """Run both docs-drift checks and print a report."""
    parser = build_parser()
    args = parser.parse_args(argv)
    root = repo_root()
    # One owner source wins, in this order. The profile path resolves token
    # KINDS; --owner-name is a person token and nothing else, and the gate
    # reports what actually resolved either way.
    owner_tokens: list[str] | None = None
    owner_profile = OwnerProfileTokens()
    owner_identity_skip_reason: str | None = None
    if args.owner_name:
        owner_tokens = []
        owner_profile = OwnerProfileTokens(
            person_names=[args.owner_name],
            kind_reasons={"contact": "--owner-name supplies a person name only: no email or phone token"},
        )
    elif args.owner_identity_file is not None:
        if not args.owner_identity_file.is_file():
            parser.error(f"--owner-identity-file not found: {args.owner_identity_file}")
        owner_tokens = read_token_file(args.owner_identity_file)
    elif args.owner_profile is not None:
        if not args.owner_profile.exists():
            parser.error(f"--owner-profile not found: {args.owner_profile}")
        owner_tokens = []
        owner_profile = read_owner_profile(args.owner_profile)
        owner_identity_skip_reason = owner_profile.read_error or owner_profile.skip_reason

    citations = check_citations(root)
    private_scan = check_private_names(
        root,
        args.internal_names_file,
        owner_tokens=owner_tokens,
        owner_profile=owner_profile,
        owner_identity_skip_reason=owner_identity_skip_reason,
        require_owner_identity=not args.allow_missing_owner_identity,
    )

    report_json = to_json(citations, private_scan)
    if args.json_out is not None:
        args.json_out.write_text(report_json + "\n", encoding="utf-8")
    print(report_json if args.json else to_text(citations, private_scan))
    return 0 if overall_ok(citations, private_scan) else 1
