"""Tests for `mock-where-real-needed` rule."""

from __future__ import annotations


def test_unittest_mock_in_production_fires(run_engine):
    """`from unittest.mock import MagicMock` in backend/ fires."""
    src = (
        "from unittest.mock import MagicMock\n"
        "\n"
        "def make_stub():\n"
        "    return MagicMock()\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="mock-where-real-needed"
    )
    assert len(findings) == 1


def test_import_unittest_mock_fires(run_engine):
    """`import unittest.mock` in backend/ fires."""
    src = (
        "import unittest.mock\n"
        "\n"
        "def f():\n"
        "    return unittest.mock.MagicMock()\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="mock-where-real-needed"
    )
    assert len(findings) == 1


def test_in_test_path_does_not_fire(run_engine):
    """Mock inside tests/ is fine."""
    src = (
        "from unittest.mock import MagicMock\n"
        "\n"
        "def test_x():\n"
        "    m = MagicMock()\n"
        "    assert m is not None\n"
    )
    findings = run_engine(
        "backend/tests/test_x.py", src, rule_id="mock-where-real-needed"
    )
    assert findings == []


def test_in_conftest_does_not_fire(run_engine):
    """Mocks in conftest.py are fine."""
    src = (
        "from unittest.mock import patch\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture\n"
        "def mocked_x():\n"
        "    with patch('x') as m:\n"
        "        yield m\n"
    )
    findings = run_engine(
        "backend/conftest.py", src, rule_id="mock-where-real-needed"
    )
    assert findings == []
