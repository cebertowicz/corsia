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
    from core.corrections_recording import active_corrections_version
    from jobs.corrections_activation import complete_backfill

    version = _version()

    assert active_corrections_version() is None
    assert complete_backfill(
        version, owner_user_id="owner", weeks=13, before_count=None, after_count=4,
    )
    assert active_corrections_version() == version
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


async def _in_db(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_notice_is_marked_only_after_acceptance_and_is_not_resent(isolated_db, monkeypatch):
    import asyncio

    from events import delivery
    from jobs import corrections_activation

    version = _version()
    assert corrections_activation.complete_backfill(
        version, owner_user_id="owner", weeks=13, before_count=1.0, after_count=2.0,
    )
    monkeypatch.setattr(corrections_activation, "workspace_conductor_contact", None, raising=False)
    monkeypatch.setattr(
        "core.agent_roster_queries.workspace_conductor_contact",
        lambda _owner, _db: ({"id": "conductor"}, None),
    )
    calls = []

    async def rejected(*args, **kwargs):
        calls.append((args, kwargs))
        return False

    monkeypatch.setattr(delivery, "deliver_to_thread", rejected)
    asyncio.run(corrections_activation.tell_conductor_about_activation(version))
    assert len(calls) == 1

    async def accepted(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(delivery, "deliver_to_thread", accepted)
    asyncio.run(corrections_activation.tell_conductor_about_activation(version))
    asyncio.run(corrections_activation.tell_conductor_about_activation(version))

    assert len(calls) == 2


def test_core_correction_recording_has_no_import_from_jobs():
    import ast
    from pathlib import Path

    core = Path(__file__).parents[1] / "core"
    for path in core.glob("corrections_*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = (
                [node.module or ""] if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else []
            )
            assert not [name for name in modules if name == "jobs" or name.startswith("jobs.")], path.name
