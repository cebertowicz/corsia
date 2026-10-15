from datetime import datetime, timezone


def _version(version="v1"):
    from core.corrections_recording import CheckerVersion

    return CheckerVersion(
        "corrections-classifier", version, "correction", 0.595681,
        "modernbert-owner-context-v2-2048",
    )


def test_backfill_window_uses_measurement_baseline_plus_current_period():
    from api.measurement_agents import BASELINE_PERIODS
    from jobs.corrections_activation import measurement_window_start

    now = datetime(2026, 10, 14, tzinfo=timezone.utc)

    assert (now - measurement_window_start(now)).days == (BASELINE_PERIODS + 1) * 7


def test_backfill_switches_the_active_version_once_after_completion(isolated_db):
    from jobs.corrections_activation import _active_version_key, complete_backfill

    version = _version()

    assert _active_version_key() is None
    assert complete_backfill(
        version, owner_user_id="owner", weeks=13, before_count=None, after_count=4,
    )
    assert _active_version_key() == (
        "corrections-classifier:v1:correction:0.595681:modernbert-owner-context-v2-2048"
    )
    assert not complete_backfill(
        version, owner_user_id="owner", weeks=13, before_count=None, after_count=4,
    )


def test_measurement_helper_keeps_old_version_until_new_backfill_completes(isolated_db):
    from core.corrections_recording import active_corrections_version
    from jobs.corrections_activation import complete_backfill

    old = _version("v1")
    new = _version("v2")

    assert complete_backfill(old, owner_user_id="owner", weeks=13, before_count=None, after_count=4)
    assert active_corrections_version() == old
    # A verified but in-progress v2 has no activation record, so Measurement stays on v1.
    assert active_corrections_version() != new
    assert complete_backfill(new, owner_user_id="owner", weeks=13, before_count=4, after_count=5)
    assert active_corrections_version() == new
    assert not complete_backfill(new, owner_user_id="owner", weeks=13, before_count=4, after_count=5)
    assert active_corrections_version() == new
