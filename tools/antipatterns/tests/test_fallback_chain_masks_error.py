"""Tests for `fallback-chain-masks-error` rule."""

from __future__ import annotations


def test_chained_get_or_get_or_default_fires(run_engine):
    """`d.get(a) or d.get(b) or 'fallback'` — masks intermediate falsy."""
    src = (
        "def lookup(d):\n"
        "    return d.get('primary') or d.get('secondary') or 'fallback'\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="fallback-chain-masks-error"
    )
    assert len(findings) == 1


def test_try_returning_real_data_fires(run_engine):
    """try/except returning a non-empty fallback fires."""
    src = (
        "def fetch():\n"
        "    try:\n"
        "        return real_api_call()\n"
        "    except Exception:\n"
        "        return {'cached': True}\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="fallback-chain-masks-error"
    )
    assert len(findings) == 1


def test_get_with_explicit_default_does_not_fire(run_engine):
    """`d.get(a, default)` — explicit per-call default is the safe form."""
    src = (
        "def lookup(d):\n"
        "    return d.get('primary', 'fallback')\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="fallback-chain-masks-error"
    )
    assert findings == []


def test_single_or_fallback_does_not_fire(run_engine):
    """`value or fallback` — one level of `or` is the standard idiom."""
    src = (
        "def name(user):\n"
        "    return user.display_name or 'anonymous'\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="fallback-chain-masks-error"
    )
    assert findings == []
