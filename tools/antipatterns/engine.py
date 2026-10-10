"""Rule engine for the anti-pattern detector."""

from __future__ import annotations

import ast
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .config import (
    global_exclude_globs,
    rule_settings,
    test_path_globs,
)
from .findings import Finding
from .rules import discover_rules
from .rules._base import Rule, RuleContext


@dataclass
class Engine:
    """Apply a configured rule set to a list of files."""

    rules: list[Rule]
    config: dict
    repo_root: Path

    @classmethod
    def from_config(
        cls,
        config: dict,
        repo_root: Path | str | None = None,
        rules: Sequence[Rule] | None = None,
    ) -> "Engine":
        """Build an engine with merged config and the standard rule set.

        Args:
            config: Merged config dict (see `config.load_config`).
            repo_root: Path to repo root for relativizing file paths.
            rules: Optional override of the rule list (used by tests).
        """
        if repo_root is None:
            repo_root = Path.cwd()
        rule_list = list(rules) if rules is not None else discover_rules()
        active: list[Rule] = []
        for rule in rule_list:
            settings = rule_settings(config, rule.id)
            if not settings.get("enabled", True):
                continue
            severity_override = settings.get("severity")
            if severity_override and severity_override != rule.severity:
                rule = Rule(
                    id=rule.id,
                    severity=severity_override,
                    description=rule.description,
                    false_positive_examples=rule.false_positive_examples,
                    fix_hint=rule.fix_hint,
                    check=rule.check,
                    calibration_note=rule.calibration_note,
                )
            active.append(rule)
        return cls(rules=active, config=config, repo_root=Path(repo_root))

    def run(self, paths: Iterable[str | Path]) -> list[Finding]:
        """Run all enabled rules against the given file paths.

        Paths may be absolute or relative. Files outside the repo root are
        skipped silently (out-of-scope for the gate).
        """
        findings: list[Finding] = []
        exclude = global_exclude_globs(self.config)
        test_globs = test_path_globs(self.config)
        for path in paths:
            abs_path = Path(path).resolve()
            try:
                rel = abs_path.relative_to(self.repo_root)
            except ValueError:
                continue
            rel_str = str(rel)
            if _matches_any(rel_str, exclude):
                continue
            if not abs_path.is_file():
                continue
            try:
                source = abs_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            ctx = self._build_context(rel_str, source, test_globs)
            for rule in self.rules:
                settings = rule_settings(self.config, rule.id)
                include_globs = settings.get("include_globs")
                if include_globs and not _matches_any(rel_str, include_globs):
                    continue
                rule_excludes = settings.get("exclude_globs", [])
                if rule_excludes and _matches_any(rel_str, rule_excludes):
                    continue
                try:
                    for finding in rule.check(ctx):
                        if finding.severity != rule.severity:
                            finding = Finding(
                                rule_id=finding.rule_id,
                                severity=rule.severity,
                                file=finding.file,
                                line=finding.line,
                                col=finding.col,
                                snippet=finding.snippet,
                                message=finding.message,
                                fix_hint=finding.fix_hint,
                                extra=finding.extra,
                            )
                        findings.append(finding)
                except Exception as exc:  # noqa: BLE001 — engine must not crash on rule errors
                    findings.append(
                        Finding(
                            rule_id=rule.id,
                            severity="warning",
                            file=rel_str,
                            line=1,
                            col=None,
                            snippet="",
                            message=(
                                f"rule {rule.id} raised {type(exc).__name__}: "
                                f"{exc}. This is a rule bug, not author code "
                                "to fix; please file."
                            ),
                            fix_hint=None,
                        )
                    )
        return findings

    def _build_context(
        self, rel_path: str, source: str, test_globs: list[str]
    ) -> RuleContext:
        """Parse once per file, share with every rule."""
        is_python = rel_path.endswith(".py")
        ast_tree: ast.AST | None = None
        if is_python:
            try:
                ast_tree = ast.parse(source, filename=rel_path)
            except SyntaxError:
                ast_tree = None
        return RuleContext(
            file_path=rel_path,
            source=source,
            lines=source.splitlines(),
            ast_tree=ast_tree,
            is_python=is_python,
            is_test_path=_matches_any(rel_path, test_globs),
            config=self.config,
        )


def _matches_any(path: str, globs: Iterable[str]) -> bool:
    """Return True if `path` matches any of the given globs.

    Supports recursive globs plus basename fallbacks.
    """
    from pathlib import PurePosixPath

    pure = PurePosixPath(path)
    basename = path.rsplit("/", 1)[-1]
    for g in globs:
        try:
            if pure.full_match(g):  # py3.13+
                return True
        except (AttributeError, ValueError):
            if g.startswith("**/") and fnmatch.fnmatch(path, g[3:]):
                return True
            simplified = g.replace("**/", "*/").replace("/**", "/*")
            if fnmatch.fnmatch(path, simplified):
                return True
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(basename, g):
            return True
    return False
