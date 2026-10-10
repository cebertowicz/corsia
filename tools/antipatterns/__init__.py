"""Anti-pattern detector for CORSIA.

Detects common code anti-patterns at commit/test time. Write-time prevention
belongs to CORSIA's bootloaded engineering and SDLC skills. Ships 6 initial
rule classes; rules grow with usage.

Public API:
    Finding   — dataclass for a single detector hit (see findings.py).
    Rule      — schema for a single rule module (see rules/_base.py).
    Engine    — runs configured rules over a file list (see engine.py).
    load_config — reads default + per-repo config (see config.py).

Layout:
    rules/      — one module per rule; each exposes a `RULE: Rule` instance.
    cli.py      — `python -m antipatterns` entry point.
    engine.py   — rule runner.
    findings.py — Finding schema + JSON serialization.
    config.py   — config loader (default-config.toml + .antipatterns.toml).

This package implements the configured proactive anti-pattern checks.
Retroactive checks live in the extended code-cleanup audit (out of scope here).
"""

from .findings import Finding
from .rules._base import Rule
from .engine import Engine
from .config import load_config

__all__ = ["Finding", "Rule", "Engine", "load_config"]
