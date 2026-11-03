"""Tests for the mypy regression gate (scripts/mypy_error_limits.py)."""

from __future__ import annotations

import pytest


def test_summary_count_is_read():
    import mypy_error_limits as module

    report = "a.py:1: error: x  [misc]\nFound 3 errors in 2 files (checked 10 source files)\n"
    assert module.count_errors(report) == 3


def test_blocking_error_fails_instead_of_reading_as_a_drop():
    """mypy stopping on a stub it cannot parse reports 1 error; that is a failed run, not an improvement."""
    import mypy_error_limits as module

    report = (
        ".venv/lib/python3.12/site-packages/numpy/__init__.pyi:737: error: Invalid syntax  [syntax]\n"
        "Found 1 error in 1 file (errors prevented further checking)\n"
    )
    with pytest.raises(SystemExit, match="stopped early"):
        module.count_errors(report)
