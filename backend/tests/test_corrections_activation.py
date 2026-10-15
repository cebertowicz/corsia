from datetime import datetime, timezone


def test_backfill_window_uses_measurement_baseline_plus_current_period():
    from api.measurement_agents import BASELINE_PERIODS
    from jobs.corrections_activation import measurement_window_start

    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    assert (now - measurement_window_start(now)).days == (BASELINE_PERIODS + 1) * 7
