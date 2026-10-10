"""Config loader for the anti-pattern detector.

Layered config:
    1. Built-in defaults from `default-config.toml` (shipped with the package).
    2. Per-repo overrides from `.antipatterns.toml` at repo root, if present.

Top-level shape:
    [global]
    exclude_globs = ["**/__pycache__/**", "**/.git/**", ...]

    [rules.<rule_id>]
    enabled = true
    severity = "warning"            # override the rule's default
    extra = { ... }                 # rule-specific tuning

Precedence: per-repo settings shallow-merge over defaults, key by key. Lists
replace, not append (tuning is explicit; no surprise additive behavior).

Python 3.11+ stdlib `tomllib` is used (read-only TOML parsing).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


_DEFAULT_CONFIG_NAME = "default-config.toml"
_REPO_CONFIG_NAME = ".antipatterns.toml"


def _read_toml(path: Path) -> dict:
    """Load a TOML file, returning {} if the file is missing."""
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _shallow_merge(base: dict, overlay: dict) -> dict:
    """Merge `overlay` into a copy of `base`.

    Two-deep strategy: top-level keys merge; for any key whose value is a
    dict in BOTH base and overlay, do a one-level dict merge. Lists in
    overlay replace base lists. Anything else: overlay wins.
    """
    out: dict[str, Any] = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def load_config(repo_root: Path | str | None = None) -> dict:
    """Load merged config: built-in defaults + per-repo overrides.

    Args:
        repo_root: Absolute path to the repo root. If None, the current
            working directory is used (matches pre-commit's invocation
            convention — pre-commit sets cwd to repo root).

    Returns:
        The merged config dict. Always present keys: `global`, `rules`.
    """
    package_dir = Path(__file__).resolve().parent
    default_config = _read_toml(package_dir / _DEFAULT_CONFIG_NAME)
    if repo_root is None:
        repo_root = Path.cwd()
    repo_root = Path(repo_root)
    repo_config = _read_toml(repo_root / _REPO_CONFIG_NAME)
    merged = _shallow_merge(default_config, repo_config)
    # Deep merge per-rule sections so a repo can override one rule's severity
    # without redeclaring every field.
    base_rules = default_config.get("rules", {})
    overlay_rules = repo_config.get("rules", {})
    merged_rules: dict[str, dict] = {}
    for rule_id in set(base_rules) | set(overlay_rules):
        merged_rules[rule_id] = _shallow_merge(
            base_rules.get(rule_id, {}),
            overlay_rules.get(rule_id, {}),
        )
    merged["rules"] = merged_rules
    merged.setdefault("global", {})
    return merged


def rule_settings(config: dict, rule_id: str) -> dict:
    """Return the merged settings for a single rule.

    Defaults: `{"enabled": True}`. Severity, exclude_globs, and any
    rule-specific knobs are filled in from config when present.
    """
    section = config.get("rules", {}).get(rule_id, {})
    out = {"enabled": True}
    out.update(section)
    return out


def global_exclude_globs(config: dict) -> list[str]:
    """Return the list of file globs to skip across all rules."""
    return list(config.get("global", {}).get("exclude_globs", []))


def test_path_globs(config: dict) -> list[str]:
    """Return the globs that mark a file as a test path.

    Used by rules like `mock-where-real-needed` to skip test files.
    """
    return list(config.get("global", {}).get("test_path_globs", []))
