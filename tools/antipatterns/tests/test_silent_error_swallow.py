"""Tests for `silent-error-swallow` rule.

Each test pairs a true-positive (TP — rule fires) with a false-positive
guard (FP — rule must NOT fire). Severity assertions test the engine's
per-config overlay.
"""

from __future__ import annotations


def test_bare_except_pass_fires(run_engine):
    """`except: pass` is the canonical silent swallow."""
    src = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except:\n"
        "        pass\n"
    )
    findings = run_engine("backend/foo.py", src, rule_id="silent-error-swallow")
    assert len(findings) == 1
    assert findings[0].rule_id == "silent-error-swallow"
    assert "pass" in findings[0].message.lower() or "silent" in findings[0].message.lower()


def test_except_exception_pass_fires(run_engine):
    """`except Exception: pass` swallows broadly."""
    src = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    findings = run_engine("backend/foo.py", src, rule_id="silent-error-swallow")
    assert len(findings) == 1


def test_debug_log_only_fires(run_engine):
    """Handler that only logs at .debug(...) — silent in prod."""
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        logger.debug('boom')\n"
    )
    findings = run_engine("backend/foo.py", src, rule_id="silent-error-swallow")
    assert len(findings) == 1


def test_return_none_fires(run_engine):
    """`except Exception: return None` hides the failure."""
    src = (
        "def f():\n"
        "    try:\n"
        "        return risky()\n"
        "    except Exception:\n"
        "        return None\n"
    )
    findings = run_engine("backend/foo.py", src, rule_id="silent-error-swallow")
    assert len(findings) == 1


def test_logger_exception_then_raise_does_not_fire(run_engine):
    """`except Exception: logger.exception(...); raise` — re-raised."""
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        logger.exception('boom')\n"
        "        raise\n"
    )
    findings = run_engine("backend/foo.py", src, rule_id="silent-error-swallow")
    assert findings == []


def test_narrow_except_does_not_fire(run_engine):
    """`except KeyError: <recovery>` — narrow, intentional."""
    src = (
        "def f(d):\n"
        "    try:\n"
        "        return d['x']\n"
        "    except KeyError:\n"
        "        return compute_default()\n"
    )
    findings = run_engine("backend/foo.py", src, rule_id="silent-error-swallow")
    assert findings == []
