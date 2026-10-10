"""Process owner of loaded document identities, lanes and owed publishers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import threading
from weakref import WeakValueDictionary
from uuid import uuid4

from doceditor.live_failure_record import SetupFailure

from doceditor.document_save import validate_live_write, write_atomic_plain
from doceditor import live_events
from doceditor.live_damage import DamageSave
from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_admission import OpenRefused, falls_back_to_plain, open_refusal, store_refusal
from doceditor.live_backpressure import PublicationGate
from doceditor.live_lane import DocumentLane
from doceditor.live_publisher import DocumentPublisher
from doceditor.live_publication_record import WriteActor
from doceditor.live_shared_policy import SharedLivePolicy
from doceditor.live_setup import setup_shared
from doceditor.live_store import LiveStore, StoreRefused
from infra.guidance_git import guidance_target


log = logging.getLogger(__name__)
LIVE_ROW_BOUND = 64
"""Compact after promote above 64 rows. On the 1MB fixture, 64 full-state
resends replayed in at most 36.1ms; 128 took 71.2ms. Keep 50ms margin to the
100ms heartbeat budget. This is a host measurement, not a universal bound.
"""


@dataclass
class DocumentLifetime:
    """One path's admission and independent final cleanup, never a global wait."""
    lock: asyncio.Lock
    closing: asyncio.Task | None = None


@dataclass
class LiveEntry:
    live: LiveDocument
    lane: DocumentLane
    publisher: DocumentPublisher
    artifact_id: str = ""
    policy: SharedLivePolicy | None = None
    lifetime: DocumentLifetime | None = None


class LiveDocuments:
    """One identity owner per process; publication remains independent of viewers."""

    def __init__(self, root: Path, *, workspace: Path, db_path: Path):
        self.root = root
        self.workspace = workspace
        self.db_path = db_path
        self.documents: dict[str, LiveEntry] = {}
        self.publishers: dict[Path, DocumentPublisher] = {}
        # Content paths whose store refused to open; legacy saves and agent writes read it.
        self.refused_stores: dict[Path, dict[str, str]] = {}
        self._lifetimes: WeakValueDictionary[Path, DocumentLifetime] = WeakValueDictionary()
        self._admission: WeakValueDictionary[Path, asyncio.Lock] = WeakValueDictionary()
        self._opening: set[Path] = set()
        self.startup_task: asyncio.Task | None = None
        self._closed = False
        self.setup_failures: dict[Path, SetupFailure] = {}
        self.setup_incarnation = str(uuid4())
        self._setup_revision = 0
        self._setup_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def setup_shared(self, path: Path, actor: WriteActor, *, db=None) -> None:
        """Common validated-open, independent-write and Retry identity setup."""
        setup_shared(self, path, actor, db=db)

    def setup_changed(self, path: Path, reason: str) -> None:
        """Store one completion-ordered process fact and deliver its visible change."""
        with self._setup_lock:
            previous = self.setup_failures.get(path)
            if (previous.reason if previous else "") == reason:
                return
            self._setup_revision += 1
            revision = self._setup_revision
            if reason:
                self.setup_failures[path] = SetupFailure(reason, revision)
            else:
                self.setup_failures.pop(path, None)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._setup_notice, path, reason, revision)

    def setup_state(self, path: Path) -> tuple[SetupFailure | None, int]:
        """Observe the process fact and absence watermark under its canonical lock."""
        with self._setup_lock:
            return self.setup_failures.get(path), self._setup_revision

    def _setup_notice(self, path: Path, reason: str, revision: int) -> None:
        provenance = {"setup_incarnation": self.setup_incarnation,
                      "setup_revision" if reason else "clears_through": revision}
        for entry in self.documents.values():
            if entry.live.path == path:
                entry.live.failure_changed("setup_failed", reason, provenance)

    def admission_lock(self, path: Path) -> asyncio.Lock:
        """Order legacy check-through-write with this path becoming live.

        Both callers hold a strong reference while waiting or working. Idle
        paths leave no permanent lock record; all paths are already canonical.
        """
        lock = self._admission.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._admission[path] = lock
        return lock

    def _lifetime(self, path: Path) -> DocumentLifetime:
        owner = self._lifetimes.get(path)
        if owner is None:
            owner = DocumentLifetime(self.admission_lock(path))
            self._lifetimes[path] = owner
        return owner

    def _publisher(self, store: LiveStore) -> DocumentPublisher:
        publisher = self.publishers.get(store.path)
        if publisher is None:
            publisher = DocumentPublisher(store)
            publisher.on_drained = lambda: self._drop_drained_publisher(store.path, publisher)
            self.publishers[store.path] = publisher
        publisher.require_running()
        return publisher

    def _has_live_or_opening(self, state_path: Path) -> bool:
        return state_path in self._opening or any(
            entry.live.store.path == state_path for entry in self.documents.values()
        )

    def _drop_drained_publisher(self, state_path: Path, publisher: DocumentPublisher) -> None:
        """Retire an idle publisher; stopped owners keep their debt only on disk."""
        if self._has_live_or_opening(state_path):
            return
        joined_stop = publisher.closed and (publisher.task is None or publisher.task.done())
        if not publisher.drained and not joined_stop:
            return
        if self.publishers.get(state_path) is publisher:
            retired = self.publishers.pop(state_path, None)
            if retired is not None:
                retired.stop()

    def _release_opening(self, state_path: Path) -> None:
        self._opening.discard(state_path)
        publisher = self.publishers.get(state_path)
        if publisher is not None:
            self._drop_drained_publisher(state_path, publisher)

    async def startup(self) -> None:
        """Schedule one owed-only publication recovery after runtime readiness."""
        self._loop = asyncio.get_running_loop()
        if self.startup_task is None:
            self.startup_task = asyncio.create_task(self._startup())

    async def _startup(self) -> None:
        """Probe stores read-only, retaining publishers only for healthy owed rows."""
        paths = await asyncio.to_thread(lambda: tuple(self.root.glob("*.sqlite")))
        for path in paths:
            if self._closed:
                return
            try:
                owed = await asyncio.to_thread(LiveStore.probe_owed, path)
            except StoreRefused as error:
                await _report_refused("startup", path, error)
                continue
            if not owed:
                continue
            try:
                store = await asyncio.to_thread(LiveStore, path)
            except StoreRefused as error:
                await _report_refused("startup", path, error)
                continue
            if self._closed:
                return
            self._publisher(store).notify()

    async def open(self, artifact_id: str, path: Path) -> LiveEntry | None:
        """Load a transport-authorized existing document; guidance stays legacy.

        Identity setup has already run in the transport's validated load.
        """
        self._loop = asyncio.get_running_loop()
        if await asyncio.to_thread(guidance_target, path, self.workspace) is not None:
            return None
        owner = self._lifetime(path)
        if owner.closing is not None:
            await asyncio.shield(owner.closing)
        async with owner.lock:
            # Cleanup may have started while this opener was queued on the lock.
            refused = open_refusal(self.documents, artifact_id, path, owner)
            if refused is not None:
                raise refused
            existing = self.documents.get(artifact_id)
            if existing is not None:
                try:
                    existing.publisher.require_running()
                except StoreRefused as error:
                    refusal = await _report_refused("open", existing.live.store.path, error)
                    raise refusal from error
                return existing
            # Artifact identifiers are opaque, so no caller can select a state path.
            name = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest() + ".sqlite"
            state_path = self.root / name
            self._opening.add(state_path)
            try:
                live = await LiveDocument.open(path, state_path)
            except StoreRefused as error:
                # The store's bytes stay as they are: they may hold acknowledged typing.
                refusal = await self._refused_open(path, state_path, error)
                raise refusal from error
            except BaseException:
                self._release_opening(state_path)
                raise
            self.refused_stores.pop(path, None)
            try:
                publisher = self._publisher(live.store)
            except BaseException:
                live.close()
                self._release_opening(state_path)
                raise
            policy = SharedLivePolicy(live, owner=self)
            def replace_file(text, prior_hash):
                write_atomic_plain(path, text, prior_hash)
                return file_hash(text.encode("utf-8"))
            async def writer(text, prior_hash):
                live.on_disk = (await asyncio.to_thread(replace_file, text, prior_hash), text)
            async def validate_actor(text, caller):
                await asyncio.to_thread(validate_live_write, path, text, caller)
            lane = DocumentLane(live, validate_actor=validate_actor, write=writer,
                                policy=policy.decide, published=publisher.notify, artifact_id=artifact_id,
                                admission=PublicationGate(publisher), promoted=lambda: self._compact(live))
            policy.schedule_setup = lane.schedule_setup
            try:
                await lane.load()
                entry = LiveEntry(live, lane, publisher, artifact_id, policy, owner)
                from doceditor.live_comments import positions
                await positions(entry, artifact_id, reload=True)
            except BaseException:
                owner.closing = asyncio.create_task(self._finish_failed_open(owner, lane, live))
                await asyncio.shield(owner.closing)
                raise
            self.documents[artifact_id] = entry
            self._release_opening(state_path)
            # Every store object this document's work reads reports damage to one save owner.
            live.store.observe_refusal = publisher.store.observe_refusal = DamageSave(entry).observe
            publisher.notify()
            live_events.emit("live_opened", artifact_id=artifact_id, store_id=live.store.store_id)
            return entry

    async def _refused_open(self, path: Path, state_path: Path, error: StoreRefused) -> OpenRefused:
        """Join retained publication before releasing a store classified damaged."""
        publisher = self.publishers.get(state_path)
        try:
            if publisher is not None and falls_back_to_plain(store_refusal(error).kind):
                publisher.stop(error)
                await publisher.close()
        finally:
            self._release_opening(state_path)
        refusal = await _report_refused("open", state_path, error)
        self.refused_stores[path] = refusal.detail()
        return refusal

    async def _compact(self, live: LiveDocument, *, force=False) -> None:
        """The lifecycle owner's only journal squash: after promote or final flush.

        Snapshot durable state under the same lock that commits browser batches.
        Received but unacknowledged text is never promoted by compaction.
        """
        async with live.committer_lock:
            try:
                if await asyncio.to_thread(live.store.pending) is not None:
                    return
                if force or await asyncio.to_thread(live.store.live_count) > LIVE_ROW_BOUND:
                    await asyncio.to_thread(live.store.compact, live.durable.get_update())
            except StoreRefused:
                # A landed write stays landed; retained journal rows are retried at
                # the next promote/eviction, while the existing refusal hook sees damage.
                log.error("Document journal compaction refused; durable rows retained", exc_info=True)

    async def _finish_failed_open(self, owner, lane, live) -> None:
        try:
            await lane.close()
        finally:
            live.close()
            self._release_opening(live.store.path)
            owner.closing = None

    async def evict(self, artifact_id: str, entry: LiveEntry, *, can_evict: Callable[[], bool],
                    before_close: Callable[[], Awaitable] | None = None) -> None:
        """The path owner completes cleanup even when its last viewer is cancelled."""
        owner = entry.lifetime or self._lifetime(entry.live.path)
        if owner.closing is None or owner.closing.done():
            owner.closing = asyncio.create_task(self._retire(owner, artifact_id, entry, can_evict, before_close))
        await asyncio.shield(owner.closing)

    async def _retire(self, owner: DocumentLifetime, artifact_id: str, entry: LiveEntry,
                      can_evict, before_close) -> None:
        async with owner.lock, entry.live.comment_mutex:
            if self.documents.get(artifact_id) is not entry or not can_evict():
                return
            try:
                if before_close is not None:
                    await before_close()
            except Exception as error:
                log.error("Document close left uncommitted updates unacknowledged for client resend: %s", error)
            finally:
                if can_evict():
                    try:
                        await entry.lane.close()
                        await self._compact(entry.live, force=True)
                    finally:
                        # Compaction can itself classify damage and stop this owner.
                        if entry.publisher.closed:
                            await entry.publisher.close()
                        self.documents.pop(artifact_id, None)
                        self._drop_drained_publisher(entry.live.store.path, entry.publisher)
                        entry.publisher.store.observe_refusal = lambda error: None
                        entry.live.close()

    async def close(self) -> None:
        """Finish owned tasks and stop publication retries on application shutdown."""
        self._closed = True
        if self.startup_task is not None:
            await asyncio.shield(self.startup_task)
        for owner in tuple(self._lifetimes.values()):
            if owner.closing is not None:
                await asyncio.shield(owner.closing)
        for entry in tuple(self.documents.values()):
            await self.evict(entry.artifact_id, entry, can_evict=lambda: True,
                             before_close=entry.live.commit_received)
        for publisher in tuple(self.publishers.values()):
            await publisher.close()
        self.documents.clear()
        self.publishers.clear()


async def _report_refused(phase: str, state_path: Path, error: StoreRefused) -> OpenRefused:
    refusal = store_refusal(error)
    log.error("Document store refused at %s (%s); bytes kept: %s: %s", phase, refusal.kind, state_path, error)
    from infra.product_events import emit_product_event
    await asyncio.to_thread(emit_product_event, "documents", refusal.kind,
                            properties={"phase": phase, "store": state_path.name, "reason": str(error)})
    return refusal


_manager: LiveDocuments | None = None


def get_documents() -> LiveDocuments | None:
    """Return the running instance's owner; unit tests may have no live runtime."""
    return _manager


async def start_documents() -> None:
    """Start document ownership after application storage initialization."""
    from infra import config
    global _manager
    _manager = LiveDocuments(config.STATE_ROOT / "documents", workspace=config.WORKSPACE, db_path=config.DB_PATH)
    await _manager.startup()


async def close_documents() -> None:
    """Called by canonical shutdown only after all admitted turns have joined."""
    global _manager
    if _manager is not None:
        await _manager.close()
        _manager = None
