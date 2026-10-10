"""Tests for `missing-public-docstring` rule."""

from __future__ import annotations


def test_public_function_without_docstring_fires(run_engine):
    """`def foo(): ...` with no docstring fires."""
    src = (
        "def public_thing():\n"
        "    return 42\n"
    )
    findings = run_engine(
        "backend/foo.py",
        src,
        rule_id="missing-public-docstring",
        config_overlay={"rules": {"missing-public-docstring": {"include_globs": ["**/*.py"]}}},
    )
    assert len(findings) == 1


def test_public_method_without_docstring_fires(run_engine):
    """A class method without a docstring fires."""
    src = (
        "class A:\n"
        "    def do_thing(self):\n"
        "        return 1\n"
    )
    findings = run_engine(
        "backend/foo.py",
        src,
        rule_id="missing-public-docstring",
        config_overlay={"rules": {"missing-public-docstring": {"include_globs": ["**/*.py"]}}},
    )
    assert len(findings) == 1


def test_private_function_does_not_fire(run_engine):
    """Leading-underscore name is private; no docstring required."""
    src = (
        "def _private_thing():\n"
        "    return 42\n"
    )
    findings = run_engine(
        "backend/foo.py",
        src,
        rule_id="missing-public-docstring",
        config_overlay={"rules": {"missing-public-docstring": {"include_globs": ["**/*.py"]}}},
    )
    assert findings == []


def test_dunder_does_not_fire(run_engine):
    """Dunder methods are exempt for sprint 2."""
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 0\n"
    )
    findings = run_engine(
        "backend/foo.py",
        src,
        rule_id="missing-public-docstring",
        config_overlay={"rules": {"missing-public-docstring": {"include_globs": ["**/*.py"]}}},
    )
    assert findings == []


def test_function_with_docstring_does_not_fire(run_engine):
    """Public function WITH a docstring is fine."""
    src = (
        "def public_thing():\n"
        '    """Return 42."""\n'
        "    return 42\n"
    )
    findings = run_engine(
        "backend/foo.py",
        src,
        rule_id="missing-public-docstring",
        config_overlay={"rules": {"missing-public-docstring": {"include_globs": ["**/*.py"]}}},
    )
    assert findings == []


def test_test_path_does_not_fire(run_engine):
    """Files under tests/ are exempt."""
    src = (
        "def test_something():\n"
        "    assert True\n"
    )
    findings = run_engine(
        "backend/tests/test_x.py",
        src,
        rule_id="missing-public-docstring",
        config_overlay={"rules": {"missing-public-docstring": {"include_globs": ["**/*.py"]}}},
    )
    assert findings == []
