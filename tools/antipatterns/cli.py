"""Command-line entry point for the anti-pattern detector."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .config import load_config
from .engine import Engine
from .findings import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    findings_to_json,
    render_human,
    severity_at_or_above,
)

_log = logging.getLogger(__name__)


def _add_backend_to_path() -> None:
    """Resolve the backend package and add it to ``sys.path``."""
    here = Path(__file__).resolve()
    for parent in [here.parent] + list(here.parents):
        if (parent / "backend" / "infra" / "product_events.py").exists():
            backend = str(parent / "backend")
            if backend not in sys.path:
                sys.path.insert(0, backend)
            return
    fallback = Path.home() / ".corsia" / "backend"
    if (fallback / "infra" / "product_events.py").exists():
        backend = str(fallback)
        if backend not in sys.path:
            sys.path.insert(0, backend)


def _emit_invocation_event(
    *,
    mode: str,
    files_scanned: int,
    findings_count: int,
    severity_counts: dict[str, int],
) -> None:
    """Emit ``dev_tool.antipatterns_invoked`` on tool exit."""
    if os.environ.get("CORSIA_ANALYTICS_DISABLE") == "1":
        return
    _add_backend_to_path()
    try:
        from infra.product_events import emit_product_event  # type: ignore[import-not-found]
    except ImportError as exc:
        _log.debug("product_events not importable; skipping emit: %s", exc)
        return
    try:
        emit_product_event(
            "dev_tool",
            "antipatterns_invoked",
            properties={
                "tool": "antipatterns",
                "mode": mode,
                "files_scanned": files_scanned,
                "findings": findings_count,
                "severity_counts": dict(severity_counts),
            },
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("dev_tool.antipatterns_invoked emit failed: %s", exc)


def _detect_mode(args: argparse.Namespace) -> str:
    """Classify the invocation mode for the emit payload."""
    if os.environ.get("PRE_COMMIT") == "1":
        return "pre-commit"
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true":
        return "ci"
    if args.staged:
        return "pre-commit"
    return "manual"


def _git_staged_files(repo_root: Path) -> list[str]:
    """Return repo-relative paths of staged and modified files."""
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=ACMR",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"git diff --cached failed: {exc}") from exc
    return [line for line in result.stdout.splitlines() if line.strip()]


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for `python -m antipatterns`."""
    p = argparse.ArgumentParser(
        prog="antipatterns",
        description=(
            "Anti-pattern detector for CORSIA. Scans staged or explicit "
            "files; emits findings; non-zero exit blocks pre-commit."
        ),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--staged",
        action="store_true",
        help="Scan git-staged files (used by the pre-commit hook).",
    )
    g.add_argument(
        "--paths",
        nargs="+",
        help="Explicit list of file paths to scan.",
    )
    g.add_argument(
        "--all",
        action="store_true",
        help="Scan the entire repo (excluding global excludes). Used by "
        "the calibration corpus run.",
    )
    p.add_argument(
        "--severity",
        choices=[SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO],
        default=SEVERITY_ERROR,
        help=(
            "Minimum severity for the exit code. Default: error. Findings "
            "below the threshold still print but don't block."
        ),
    )
    p.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format. JSON for tooling; human for terminals.",
    )
    p.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help="Path to repo root (default: cwd).",
    )
    p.add_argument(
        "--rule",
        action="append",
        dest="rule_filter",
        default=None,
        help=(
            "Restrict to one or more rule ids. Repeat the flag for "
            "multiple rules. Useful for focused calibration runs."
        ),
    )
    return p


def _walk_repo(repo_root: Path, exclude_globs: list[str]) -> list[str]:
    """List every file under repo_root, skipping excluded globs."""
    import fnmatch

    out: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo_root))
        basename = rel.rsplit("/", 1)[-1]
        if any(
            fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(basename, g)
            for g in exclude_globs
        ):
            continue
        out.append(rel)
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path.cwd().resolve()
    )
    config = load_config(repo_root)
    if args.staged:
        try:
            paths = _git_staged_files(repo_root)
        except RuntimeError as exc:
            print(f"antipatterns: {exc}", file=sys.stderr)
            return 2
    elif args.all:
        paths = _walk_repo(repo_root, list(config.get("global", {}).get("exclude_globs", [])))
    else:
        paths = list(args.paths or [])
    if not paths:
        if args.format == "json":
            print(findings_to_json([]))
        else:
            print("antipatterns: no files in scope.")
        _emit_invocation_event(
            mode=_detect_mode(args),
            files_scanned=0,
            findings_count=0,
            severity_counts={"error": 0, "warn": 0},
        )
        return 0
    engine = Engine.from_config(config, repo_root=repo_root)
    if args.rule_filter:
        wanted = set(args.rule_filter)
        engine = Engine(
            rules=[r for r in engine.rules if r.id in wanted],
            config=engine.config,
            repo_root=engine.repo_root,
        )
    findings = engine.run(paths)
    blocking = severity_at_or_above(args.severity)
    blocking_findings = [f for f in findings if f.severity in blocking]
    if args.format == "json":
        print(findings_to_json(findings))
    else:
        print(render_human(findings))
    sev_counter: Counter[str] = Counter(f.severity for f in findings)
    _emit_invocation_event(
        mode=_detect_mode(args),
        files_scanned=len(paths),
        findings_count=len(findings),
        severity_counts={
            "error": sev_counter.get(SEVERITY_ERROR, 0),
            "warn": sev_counter.get(SEVERITY_WARNING, 0),
        },
    )
    return 1 if blocking_findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
