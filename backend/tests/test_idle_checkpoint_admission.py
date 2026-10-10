"""Idle checkpoint token admission policy."""

from __future__ import annotations


def test_below_the_delta_is_refused() -> None:
    from infra.checkpoint_admission import admits_idle_checkpoint

    assert admits_idle_checkpoint(32_767) is False


def test_exactly_32768_tokens_is_admitted() -> None:
    from infra.checkpoint_admission import admits_idle_checkpoint

    assert admits_idle_checkpoint(32_768) is True
