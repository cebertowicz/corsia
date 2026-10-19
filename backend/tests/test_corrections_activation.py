from datetime import datetime, timezone


def _version(version="v1"):
    from core.corrections_recording import ClassifierVersion

    return ClassifierVersion(
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


def test_activation_records_when_the_version_became_active(isolated_db):
    from core.corrections_recording import active_since
    from infra.database import get_db
    from jobs.corrections_activation import complete_backfill

    before = datetime.now(timezone.utc)
    complete_backfill(_version(), owner_user_id="owner", weeks=13, before_count=None, after_count=4)
    db = get_db()
    try:
        since = active_since(db)
        assert active_since(db, _version("v2")) is None  # only the active version has a start
    finally:
        db.close()

    assert since is not None and datetime.fromisoformat(since) >= before


def test_the_activation_figure_compares_chat_messages_only(isolated_db, monkeypatch):
    import asyncio

    from jobs import corrections_activation

    calls = []

    def figure(version, *, since, weeks, messages_only=False):
        calls.append((version.version, messages_only))
        return 1.0

    monkeypatch.setattr(corrections_activation, "weekly_correction_figure", figure)
    monkeypatch.setattr("jobs.corrections_recording.owner_user_ids", lambda: ["owner"])
    monkeypatch.setattr(corrections_activation, "tell_conductor_about_activation", lambda _version: asyncio.sleep(0))
    monkeypatch.setattr(corrections_activation, "retry_pending_activation_notice", lambda: asyncio.sleep(0))
    corrections_activation.complete_backfill(_version(), owner_user_id="owner", weeks=7, before_count=None, after_count=1)

    assert asyncio.run(corrections_activation.activate_after_backfill(_version("v2"))) != "activation record unreadable"
    assert calls == [("v1", True), ("v2", True)]


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


def test_malformed_activation_record_is_corrupt_not_absent(isolated_db):
    from core.corrections_recording import (
        ACTIVE_VERSION_SETTING, CorruptActivation, active_corrections_version,
    )
    from infra.settings import set_setting
    from jobs.corrections_activation import complete_backfill

    assert complete_backfill(_version(), owner_user_id="owner", weeks=13, before_count=None, after_count=4)
    set_setting(ACTIVE_VERSION_SETTING, {"key": "no-such-activation"})

    assert isinstance(active_corrections_version(), CorruptActivation)


def test_activation_never_replaces_a_corrupt_record_or_says_now_counted(isolated_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from core import corrections_recording as core_recording
    from jobs import corrections_activation, corrections_realtime, corrections_recording

    completed, told = [], []
    monkeypatch.setattr(corrections_recording, "owner_user_ids", lambda: ["owner"])
    monkeypatch.setattr(
        core_recording, "active_corrections_version", lambda: core_recording.CorruptActivation("KeyError"),
    )
    monkeypatch.setattr(corrections_activation, "complete_backfill", lambda *a, **k: completed.append(1))

    async def tell(version):
        told.append(version)

    monkeypatch.setattr(corrections_activation, "tell_conductor_about_activation", tell)
    manifest = SimpleNamespace(classifier_version=_version)

    outcome = asyncio.run(corrections_activation.activate_after_backfill(manifest.classifier_version()))

    assert outcome == "activation record unreadable"
    assert completed == [] and told == []


def test_conductor_figure_is_rounded_for_people(isolated_db, monkeypatch):
    import asyncio

    from events import delivery
    from jobs import corrections_activation

    version = _version()
    corrections_activation.complete_backfill(
        version, owner_user_id="owner", weeks=7, before_count=24 / 7, after_count=4.0,
    )
    monkeypatch.setattr(
        "core.agent_roster_queries.corsia_agent_contact",
        lambda _owner, _db: ({"id": "conductor"}, None),
    )
    sent = []

    async def accepted(thread_id, kind, detail, *args, **kwargs):
        sent.append(detail)
        return True

    monkeypatch.setattr(delivery, "deliver_to_thread", accepted)
    asyncio.run(corrections_activation.tell_conductor_about_activation(version))

    assert sent[0].startswith("Corrections classifier version v1 was installed and activated.")
    assert "moved from 3.4 to 4.0" in sent[0] and "compares chat messages only" in sent[0]
    assert sent[0].count("v1") == 1  # the version reads once, in the prefix


def test_notice_is_marked_only_after_acceptance_and_is_not_resent(isolated_db, monkeypatch):
    import asyncio

    from events import delivery
    from jobs import corrections_activation

    version = _version()
    assert corrections_activation.complete_backfill(
        version, owner_user_id="owner", weeks=13, before_count=1.0, after_count=2.0,
    )
    monkeypatch.setattr(corrections_activation, "corsia_agent_contact", None, raising=False)
    monkeypatch.setattr(
        "core.agent_roster_queries.corsia_agent_contact",
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
