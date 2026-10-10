"""FIFO document writes off the pump, with bounded durable closure."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
import logging
import sqlite3
from typing import TYPE_CHECKING, Literal

from doceditor import live_events
from doceditor.live_author import agent_author
from doceditor.live_candidate import build_candidate
from doceditor.live_comments import candidate_ranges
from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_store import StoreRefused
from doceditor.live_lane_record import record_candidate
from doceditor.live_publication_record import Publication, WriteActor
from doceditor.live_text import apply_text_async, clone
from doceditor.live_operation import (EditRefused as EditRefused, FileUnreadable, LaneRefused, Operation,
                                      TextEdit as TextEdit, WriteResult, is_ordinary_refusal)

if TYPE_CHECKING:
    from doceditor.live_backpressure import PublicationGate

log = logging.getLogger(__name__)
OUTSIDE_WRITE_ATTEMPTS = 3
"""Attempts before a write that keeps meeting outside writes is refused."""
NATIVE_SETTLE_TIMEOUT_S = 5.0
"""Native event settlement budget, inside its existing 10s turn join.
Real CC/Codex task cancellation propagates immediately without a lane wait.
"""


async def _checkpoint(stage: str) -> None:
    """A named write stage; tests monkeypatch this to stop a write there."""


def _report_late_failure(future) -> None:
    if not future.cancelled() and future.exception() is not None:
        log.warning("Started document write failed after its caller stopped: %s", future.exception())


@dataclass
class _Queued:
    operation: Operation | Callable[[], Awaitable]
    future: asyncio.Future
    cancel_event: asyncio.Event | None
    started: bool = False
    detached: bool = False


class DocumentLane:
    def __init__(
        self, live: LiveDocument, *, validate_actor: Callable[[str, WriteActor | None], Awaitable],
        write: Callable[[str, str], Awaitable],
        policy: Callable[[Operation, str], Awaitable[Publication | None]] | None = None,
        published: Callable[[], None] | None = None, artifact_id: str = "",
        admission: PublicationGate | None = None,
        promoted: Callable[[], Awaitable[None]] | None = None,
    ):
        self.live = live
        self.artifact_id = artifact_id
        self.validate_actor = validate_actor
        self.write = write
        self.policy = policy
        self.published = published
        self.admission = admission
        self.promoted = promoted
        self._queue: deque[_Queued] = deque()
        self._worker: asyncio.Task | None = None
        self.closed = False
        from doceditor.live_retry import LaneRecoveryRetry
        self.retry = LaneRecoveryRetry(self)

    async def submit(self, operation: Operation, *, cancel_event=None) -> WriteResult:
        try:
            return await self._submit(operation, cancel_event=cancel_event)
        except LaneRefused as refused:
            live_events.lane_outcome(self, operation, "refused")
            return WriteResult(self.live.text, False, outcome="refused", refusal=refused)

    def _enqueue(self, operation, cancel_event=None, *, recovery=False) -> _Queued:
        if self.closed:
            raise LaneRefused("The document closed; reopen it before writing")
        job = _Queued(operation, asyncio.get_running_loop().create_future(), cancel_event)
        if recovery:
            self._queue.appendleft(job)
        else:
            self._queue.append(job)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        return job

    def schedule_setup(self, setup: Callable[[], Awaitable]) -> None:
        """Enqueue independent identity setup; no tool owns or waits for this item."""
        self.schedule(setup)

    def schedule(self, item: Callable[[], Awaitable]) -> None:
        """Enqueue an independent item behind admitted operations; no tool owns or waits for it."""
        job = self._enqueue(item)
        def settled(future):
            if not future.cancelled() and future.exception() is not None:
                log.warning("Live document lane item failed: %s", future.exception())
        job.future.add_done_callback(settled)

    def _detach(self, job: _Queued) -> None:
        job.detached = True
        job.future.add_done_callback(_report_late_failure)

    def _drop(self, job: _Queued) -> WriteResult:
        if job in self._queue:
            self._queue.remove(job)
        result = WriteResult(self.live.text, False, outcome="not_started")
        if not job.future.done():
            live_events.lane_outcome(self, job.operation, "not_started")
            job.future.set_result(result)
        return result

    async def _submit(self, operation, *, cancel_event=None):
        job = self._enqueue(operation, cancel_event)
        stop = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            if stop is None:
                return await asyncio.shield(job.future)
            await asyncio.wait({job.future, stop}, return_when=asyncio.FIRST_COMPLETED)
            if job.future.done():
                return job.future.result()
            if not job.started:
                return self._drop(job)
            return await self._settle_native(job)
        except asyncio.CancelledError:
            if job.started:
                self._detach(job)
            else:
                self._drop(job)
            raise
        finally:
            if stop is not None:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)

    async def _settle_native(self, job: _Queued) -> WriteResult:
        await asyncio.wait({job.future}, timeout=NATIVE_SETTLE_TIMEOUT_S)
        if job.future.done():
            return job.future.result()
        self._detach(job)
        live_events.lane_outcome(self, job.operation, "pending", detached=True)
        return WriteResult(self.live.text, False, outcome="pending")

    async def _run(self) -> None:
        while self._queue:
            job = self._queue.popleft()
            if job.cancel_event is not None and job.cancel_event.is_set():
                self._drop(job)
                continue
            try:
                if not await self._admitted(job):
                    continue
                job.started = True
                result = (await job.operation() if callable(job.operation)
                          else await self._execute(job.operation))
            except Exception as exc:
                if job.future.done():
                    continue  # Left the queue while waiting for admission.
                ordinary = is_ordinary_refusal(exc)
                result = WriteResult(self.live.text, False, outcome="refused", refusal=exc)
                live_events.lane_outcome(self, job.operation, "refused" if ordinary else "failed",
                                         detached=job.detached)
                if ordinary and isinstance(job.operation, Operation):
                    job.future.set_result(result)
                else:
                    if job.detached and isinstance(exc, (StoreRefused, OSError, sqlite3.Error)):
                        await self.retry.record_failure(exc)
                    elif isinstance(exc, StoreRefused):
                        self.retry.notify()
                    job.future.set_exception(exc)
            else:
                if isinstance(result, WriteResult):
                    live_events.lane_outcome(self, job.operation, result.outcome, detached=job.detached,
                                             server_saved=result.server_saved)
                    if not result.server_saved:
                        self.retry.notify()
                job.future.set_result(result)

    async def _admitted(self, job: _Queued) -> bool:
        """Count an author write against owed publications before it starts.

        A refusal is settled as any refused write; one that left the queue while
        waiting settles as not started.
        """
        if self.admission is None or not isinstance(job.operation, Operation):
            return True
        if await self.admission.admit(job.future, job.cancel_event):
            return True
        self._drop(job)
        return False

    async def _barrier(self) -> None:
        pending = await asyncio.to_thread(self.live.store.pending)
        if pending is not None:
            failure = await asyncio.to_thread(self.live.store.failure, "store_refused")
            reason = self.retry.blocked_reason or (failure.message if failure else "")
            if reason:
                raise LaneRefused(reason)
            if pending.state == "held":
                raise LaneRefused("Waiting for a choice about outside changes")
            raise LaneRefused(
                "The last write to this document is not saved on the server yet; try again shortly"
            )

    async def _import(self) -> str:
        live = self.live
        data = await asyncio.to_thread(live.path.read_bytes)
        current_hash = file_hash(data)
        mirror = await asyncio.to_thread(live.store.mirror)
        if current_hash != mirror.file_hash:
            fork = clone(mirror.state)
            previous = fork.get_state()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise FileUnreadable(f"The document file on disk is not UTF-8 text: {error}") from error
            await apply_text_async(fork, text)
            update = fork.get_update(previous)
            state = fork.get_update()
            async with live.committer_lock:
                await asyncio.to_thread(live.store.commit_import, update, state, current_hash)
                await live.apply_committed(update, state, current_hash)
            live.on_disk = (current_hash, text)
        return current_hash

    # Candidate construction stays in live_candidate; durable recording in live_lane_record.

    async def _execute(self, operation: Operation) -> WriteResult:
        """Run one write; an outside write caught at the rename re-runs it on top.

        The rejected attempt replaced nothing and its pending row is discarded,
        so the re-run imports the outside text exactly as any write does.
        """
        from doceditor.document_save import FileChangedOutside
        for _ in range(OUTSIDE_WRITE_ATTEMPTS - 1):
            try:
                return await self._attempt(operation)
            except FileChangedOutside:
                log.info("Live write met an outside write before its rename; re-running on top of it")
        return await self._attempt(operation)

    async def _attempt(self, operation: Operation) -> WriteResult:
        # A landed write disproves only store failures recorded before it started.
        generation = await asyncio.to_thread(self.live.store.failure_generation)
        await self._barrier()
        prior_hash = await self._import()
        fork, accepted, skipped, receipt, author = await build_candidate(self.live, operation)
        text = str(fork["text"])
        ranges = candidate_ranges(self.live, fork)
        await self.validate_actor(text, operation.actor)
        publication = await self.policy(operation, text) if self.policy else None
        if operation.kind == "edit" and operation.multi_edit and text == self.live.text:
            self._advance_author(operation, author)
            return WriteResult(text, True, receipt=receipt)
        # Only for the typing reveal: looked up once the write is admitted, and
        # never able to fail it.
        writer = await agent_author(operation.actor, operation.kind)
        if publication is not None:
            publication = replace(publication, ranges=ranges)
        record = await record_candidate(self.live, fork, prior_hash, publication, operation)
        await _checkpoint("record")
        try:
            await self.write(text, prior_hash)
        except Exception:
            await asyncio.to_thread(self.live.store.discard, record.op_id)
            self.live.pending_changed()
            raise
        await _checkpoint("rename")
        server_saved, cleared = True, False
        try:
            await _checkpoint("promote")
            cleared = await asyncio.to_thread(self.live.store.promote, record.op_id, generation)
            self.live.pending_changed()
        except StoreRefused:
            server_saved = False
        if server_saved and cleared:
            self.retry.cleared(generation)
        if server_saved:
            # The write has landed before memory publication yields. A queued
            # call by this author must capture the landed base, not the old read.
            self._advance_author(operation, author)
            await self.live.apply_committed(record.candidate, record.mirror_state, record.result_hash, writer)
        else:
            self.live.apply_unsaved(record.candidate)
            self._advance_author(operation, author)
        if server_saved and self.published:
            self.published()
        if server_saved and self.promoted:
            await self.promoted()
        return WriteResult(text, server_saved, operation.kind == "write" and text != operation.content,
                           accepted=accepted, skipped=skipped, receipt=receipt)

    def _advance_author(self, operation: Operation, author: tuple[bytes, str]) -> None:
        # Detached writes land through this same owner, before others' edits merge.
        if operation.actor is None:
            return
        state, own_hash = author
        self.live.bases.pop(own_hash, None)
        self.live.bases[own_hash] = state
        self.live.read_bases[operation.actor.thread_id] = own_hash
        while len(self.live.bases) > self.live.base_limit:
            self.live.bases.pop(next(iter(self.live.bases)))

    async def close(self) -> None:
        """Stop retries and drain admitted operations before releasing the document."""
        self.closed = True
        self.retry.close()
        if self._worker is not None:
            await asyncio.shield(self._worker)

    async def load(self) -> None:
        """Settle recovery, then import a closed-file change before first sync."""
        from doceditor.live_recovery import recover
        async def initialize():
            if not await self.retry.exhausted():
                await recover(self)
            if await asyncio.to_thread(self.live.store.pending) is None:
                await self._import()
        await self._submit(initialize)

    async def recover(self) -> None:
        from doceditor.live_recovery import recover
        await self._submit(lambda: recover(self))

    async def choose(self, kind: Literal["corsia", "file"], *, actor: WriteActor | None = None,
                     cancel_event=None) -> WriteResult:
        from doceditor.live_recovery import choose
        return await self._submit(lambda: choose(self, kind, actor), cancel_event=cancel_event)
