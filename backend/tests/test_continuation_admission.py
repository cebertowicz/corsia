"""Deterministic restart spread tests."""

from __future__ import annotations

import pytest

from automation.continuation import admission


@pytest.mark.parametrize("thread_id", ["a", "b", "c"])
def test_restart_jitter_is_stable_and_bounded(thread_id):
    value = admission.restart_jitter_seconds(thread_id, maximum=30)
    assert value == admission.restart_jitter_seconds(thread_id, maximum=30)
    assert 0 <= value <= 30
