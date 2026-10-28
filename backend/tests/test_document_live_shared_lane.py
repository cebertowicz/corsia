"""A started shared write reads policy only and never joins identity setup."""
import asyncio
from pathlib import Path
import sqlite3
import time

import pytest

from doceditor.live_document import LiveDocument
from doceditor.live_lane import DocumentLane, Operation
from doceditor.document_save import write_atomic_plain
from registry.shared_write_errors import SharedIdentitySetupRequired
from tests.test_document_live_publisher import owed
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write, _enforce


async def opened(scope, tmp_path, actor):
    from doceditor.live_shared_policy import SharedLivePolicy
    from doceditor.live_publication_record import WriteActor
    path = scope["category"] / "live.md"
    live = await LiveDocument.open(path, tmp_path / "store.sqlite")
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    from doceditor.live_runtime import LiveDocuments
    owner = LiveDocuments(tmp_path, workspace=scope["workspace"], db_path=db_path)
    policy = SharedLivePolicy(live, owner=owner)
    async def valid(text, actor=None):
        pass
    async def writer(text, prior_hash):
        await asyncio.to_thread(write_atomic_plain, path, text, prior_hash)
    lane = DocumentLane(live, validate_actor=valid, write=writer, policy=policy.decide)
    policy.schedule_setup = lane.schedule_setup
    return live, lane, policy, WriteActor(scope[actor], "project-alpha")


@pytest.mark.asyncio
async def test_started_shared_write_has_no_app_writer_during_actual_contention(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_shared_policy
    scope = ownership_scope
    _write(scope, "live.md", "old")
    live, lane, policy, actor = await opened(scope, tmp_path, "owner")
    original = live_shared_policy.resolve_shared_target
    reads = []
    def checked(*args, **kwargs):
        db = kwargs["db"]
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 1000
        assert not db.in_transaction
        reads.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(live_shared_policy, "resolve_shared_target", checked)
    lock = sqlite3.connect(policy.db_path)
    lock.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(lane.submit(Operation("replace", content="new", actor=actor)), 10)
    finally:
        lock.rollback()
        lock.close()
    assert time.perf_counter() - started < 10
    assert result.server_saved
    assert reads == [True]
    assert len(owed(live.store)) == 1
    assert scope["db"].execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_tool_refuses_without_awaiting_setup_lane_item(ownership_scope, tmp_path):
    scope = ownership_scope
    (scope["category"] / "live.md").write_text("old")
    _enforce(scope)
    live, lane, policy, actor = await opened(scope, tmp_path, "conductor")
    entered, release = asyncio.Event(), asyncio.Event()
    real = policy.setup
    async def held(actor):
        entered.set()
        await release.wait()
        await real(actor)
    policy.setup = held
    try:
        with pytest.raises(SharedIdentitySetupRequired, match="being set up"):
            refused = await asyncio.wait_for(lane.submit(Operation("replace", content="new", actor=actor)), 1)
            assert refused.outcome == "refused"
            refused.require_landed()
        await asyncio.wait_for(entered.wait(), 1)
        assert live.path.read_text() == "old"
    finally:
        release.set()
        await lane._worker
    result = await lane.submit(Operation("replace", content="new", actor=actor))
    assert result.server_saved and live.path.read_text() == "new"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_maximum_backlog_three_store_waits_policy_under_app_writer(ownership_scope, tmp_path):
    from tests.test_document_live_performance import maximum_message
    scope = ownership_scope
    _write(scope, "live.md", "hello quote")
    live, lane, policy, actor = await opened(scope, tmp_path, "owner")
    assert live.receive(maximum_message(live))
    live.path.write_text("OUTSIDE hello quote")
    for name in ("commit_import", "record_pending", "promote"):
        original = getattr(live.store, name)
        def delayed(*args, _original=original, **kwargs):
            time.sleep(1.0)
            return _original(*args, **kwargs)
        setattr(live.store, name, delayed)
    lock = sqlite3.connect(policy.db_path)
    lock.execute("BEGIN IMMEDIATE")
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(lane.submit(Operation("flush", actor=actor)), 10)
    finally:
        lock.rollback()
        lock.close()
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert result.server_saved and live.path.read_text() == live.text
    assert len(owed(live.store)) == 1
    print(f"maximum-shared-backlog-three-waits-app-writer={elapsed:.3f}s")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_maximum_shared_flush_p99_real_policy_with_simulated_registration_drain(ownership_scope, tmp_path, monkeypatch):
    """Focused lane/policy cost under the app writer; registration and owed deletion
    are a simulated drain outside that writer. Real-runtime contention is tested
    separately through LiveDocuments with its actual publisher and admission.
    """
    from doceditor import live_publisher
    from doceditor.live_backpressure import PublicationGate
    from tests.test_document_live_performance import maximum_message
    scope = ownership_scope
    _write(scope, "live.md", "hello quote")
    live, lane, policy, actor = await opened(scope, tmp_path, "owner")
    assert live.receive(maximum_message(live))
    timings = []
    def simulated_registration_drain(store, row, **kwargs):
        store.delete_publication(row.seq)
    monkeypatch.setattr(live_publisher, "publish_one", simulated_registration_drain)
    publisher = live_publisher.DocumentPublisher(live.store)
    lane.admission = PublicationGate(publisher)
    lane.published = publisher.notify
    lock = sqlite3.connect(policy.db_path)
    lock.execute("BEGIN IMMEDIATE")
    try:
        await lane.submit(Operation("flush", actor=actor))
        for _ in range(100):
            started = time.perf_counter()
            assert (await lane.submit(Operation("flush", actor=actor))).server_saved
            timings.append(time.perf_counter() - started)
    finally:
        lock.rollback()
        lock.close()
        await lane.close()
        await publisher.close()
        live.close()
    p99 = sorted(timings)[98]
    assert p99 <= 1.0
    print(f"maximum-shared-flush-read-policy-p99={p99:.3f}s;samples=100;worst={max(timings):.3f}s")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corsia", "file"])
async def test_held_choice_publishes_choosing_actor_and_recovery_notifies(ownership_scope, tmp_path, kind):
    from doceditor.live_publication_record import WriteActor
    from doceditor.live_store import StoreRefused
    scope = ownership_scope
    _write(scope, "live.md", "old")
    live, lane, policy, actor = await opened(scope, tmp_path, "owner")
    real = live.store.promote
    def refused(*args):
        raise StoreRefused("promote refused")
    live.store.promote = refused
    await lane.submit(Operation("replace", content="candidate", actor=actor))
    live.path.write_text("outside")
    await lane.recover()
    held = live.store.pending()
    live.store.promote = real
    notifications = []
    lane.published = lambda: notifications.append(True)
    user = WriteActor(scope["plain"], "project-alpha", direct_user=True)
    result = await lane.choose(kind, actor=user)
    assert result.server_saved
    assert notifications == [True]
    row = live.store.next_publication()
    assert row.publication.actor == user.thread_id
    assert row.publication != held.publication
    assert live.path.read_text() == ("candidate" if kind == "corsia" else "outside")


@pytest.mark.asyncio
async def test_held_keep_refuses_shared_policy_without_changing_evidence(ownership_scope, tmp_path):
    from doceditor.live_store import StoreRefused
    from registry.shared_write_errors import DurableWritePolicyError
    scope = ownership_scope
    _write(scope, "live.md", "old")
    live, lane, policy, actor = await opened(scope, tmp_path, "owner")
    def refused(*args):
        raise StoreRefused("promote refused")
    live.store.promote = refused
    await lane.submit(Operation("replace", content="candidate", actor=actor))
    live.path.write_text("outside")
    await lane.recover()
    held = live.store.pending()
    _enforce(scope)
    from doceditor.live_publication_record import WriteActor
    other = WriteActor(scope["other"], "project-alpha")
    with pytest.raises(DurableWritePolicyError):
        await lane.choose("corsia", actor=other)
    assert live.store.pending() == held
    assert live.path.read_text() == "outside" and live.text == "candidate"
    assert live.store.next_publication() is None
