from __future__ import annotations

from unittest.mock import patch

from infra.transient_read import retry_transient_read


def test_transient_read_returns_first_readable_result_after_one_repoll():
    reads = iter([None, "readable"])
    logs: list[str] = []

    with patch("infra.transient_read.time.sleep") as sleep:
        result = retry_transient_read(
            lambda: next(reads),
            attempts=8,
            interval_s=4.0,
            log_label=lambda: "GitHub read unreadable",
            log_retry=logs.append,
        )

    assert result == "readable"
    assert logs == ["GitHub read unreadable, re-polling (1/8)"]
    sleep.assert_called_once_with(4.0)


def test_transient_read_returns_none_after_the_full_budget():
    reads = 0

    def unreadable() -> None:
        nonlocal reads
        reads += 1
        return None

    with patch("infra.transient_read.time.sleep") as sleep:
        result = retry_transient_read(
            unreadable,
            attempts=8,
            interval_s=4.0,
            log_label=lambda: "GitHub read unreadable",
            log_retry=lambda _message: None,
        )

    assert result is None
    assert reads == 8
    assert sleep.call_count == 7
