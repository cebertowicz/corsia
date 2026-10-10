"""Pytest fixtures for the anti-pattern detector tests.

`run_engine` is the shared fixture: synthesizes a tmp file at a given
relative path, applies the engine with a synthesized config, returns the
list of Findings.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make `import antipatterns` work without an editable install. The
# package lives at `<repo>/tools/antipatterns/`, so we add `<repo>/tools`.
_TESTS_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _TESTS_DIR.parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


@pytest.fixture
def run_engine(tmp_path):
    """Return a callable that runs the engine on a synthesized file.

    Usage:
        findings = run_engine(
            "backend/foo.py",
            "import unittest.mock\\n",
            rule_id="mock-where-real-needed",
        )

    Args of the returned callable:
        rel_path: path under tmp_path used for both writing and the
            engine's `repo_root`-relative classification.
        source: file contents to write.
        rule_id: optional; if given, only that rule runs (filter).
        config_overlay: optional dict to overlay onto the loaded config.
    """
    from antipatterns.config import load_config
    from antipatterns.engine import Engine

    def _run(rel_path: str, source: str, *, rule_id: str | None = None,
             config_overlay: dict | None = None):
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        config = load_config(tmp_path)
        if config_overlay:
            for k, v in config_overlay.items():
                if k == "rules":
                    rules = config.setdefault("rules", {})
                    for rid, settings in v.items():
                        merged = dict(rules.get(rid, {}))
                        merged.update(settings)
                        rules[rid] = merged
                else:
                    config[k] = v
        engine = Engine.from_config(config, repo_root=tmp_path)
        if rule_id:
            engine = Engine(
                rules=[r for r in engine.rules if r.id == rule_id],
                config=engine.config,
                repo_root=engine.repo_root,
            )
        return engine.run([target])

    return _run
