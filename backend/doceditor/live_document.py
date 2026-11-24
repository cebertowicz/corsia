"""One live CRDT, durable closure lock and range owner per opened document."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from doceditor.document_scope import MAX_TEXT_BYTES
from doceditor.live_store import BrowserMessage, LiveStore
from doceditor.live_states import PastStates, StateKey, state_key
from doceditor.live_text import clone, create, follow_steps, shift_spans, text_digest, utf16_delta, utf16_length
from doceditor.section_log import SectionLog

if TYPE_CHECKING:
    from doceditor.live_publication_record import WriteActor


# Most selections one socket keeps tracked before their comments exist.
RESERVATION_LIMIT = 32


class IdentityInUse(ValueError):
    """A comment id that another user's capture or an existing comment already holds."""

    #: The ``doc_error`` kind a capture refused this way is answered with, so a retry can look for its stored comment.
    kind = "identity_in_use"


@dataclass
class Reservation:
    user_id: str
    holder: object  # the socket whose capture asked for it
    span: tuple[int, int]


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def yield_loop_pass() -> None:
    """Return after one full event-loop pass, for loop latency only.

    The zero-delay timer sorts after every timer already due, so those
    callbacks run first, and the tasks they wake are queued ahead of this
    caller's resumption, whatever the caller's await depth.
    """
    loop = asyncio.get_running_loop()
    passed = loop.create_future()
    def release() -> None:
        if not passed.done():
            passed.set_result(None)
    loop.call_later(0, release)
    await passed


class LiveDocument:
    def __init__(self, path: Path, store: LiveStore, doc, durable, backlog_limit: int):
        self.path = path
        self.store = store
        self.doc = doc
        self.durable = durable
        self.committer_lock = asyncio.Lock()
        self.comment_mutex = asyncio.Lock()
        # Uncommitted browser messages by id, in arrival order.
        self.received: dict[str, BrowserMessage] = {}
        self.backlog_limit = backlog_limit
        self.backlog_bytes = 0
        self.ranges: dict[str, tuple[int, int]] = {}
        # Ranges whose text was just typed over: the rest of the word typed at their end joins them (``follow_spans``).
        self.open_ends: set[str] = set()
        # Selections a capture tracks before their comment exists, oldest first. They move with the text
        # but stay out of ``ranges``, which only comments own.
        self._reservations: dict[str, Reservation] = {}
        # The states the text held recently, oldest first, newest the current one; kept while the Text
        # observer runs, so a capture that names one of them maps its selection forward (``reserve``).
        self.states = PastStates()
        # A recent base is an in-memory merge aid, never a durability claim.
        self.base_limit = 8
        self.bases: dict[str, bytes] = {}
        # Reader hashes belong to this loaded identity, never to unloaded files.
        self.read_bases: dict[str, str] = {}
        # When each reader last read, and what changed in each section since (section writer turns).
        self.sections = SectionLog()
        self.acknowledge: Callable[[tuple[str, ...]], None] = lambda ids: None
        self.text_changed: Callable[[], None] = lambda: None
        # An empty message means the failure of that kind cleared.
        self.failure_changed: Callable[[str, str, dict], None] = lambda kind, message, provenance: None
        self.pending_changed: Callable[[], None] = lambda: None
        self.update_changed: Callable[[bytes, object, dict | None], None] = lambda update, sender, author: None
        # A streamed agent edit's provisional frame for this document's viewers; never saved.
        self.preview_changed: Callable[[dict], None] = lambda frame: None
        # The actor a last-viewer flush would use now; None when no writable viewer remains.
        self.viewer_actor: Callable[[], WriteActor | None] = lambda: None
        # Who last sent typing; an accept saves typing not yet in the file as their version first.
        self.typist: WriteActor | None = None
        # (hash, text) of the file as this session last wrote or imported it.
        self.on_disk: tuple[str, str] = ("", "")
        self._sender = None
        self._merged: list[tuple[bytes, object, dict | None]] = []
        self._author: dict | None = None
        self._text = doc["text"]
        self._before = str(self._text)
        self.snapshot: tuple[str, dict[str, tuple[int, int]]] = (self._before, {})
        # pycrdt builds a Text event's whole delta as Python objects before any
        # callback runs: for a distributed 1MB rewrite, tens of milliseconds and
        # a full GC pass on the loop. Only range mapping and the recent states
        # read it, so the Text observer starts at the first tracked range or
        # capture ask (a refused one too) and stays until ``close``. From then
        # on each merge also reads the state vector and hashes the whole text
        # (``_current_key``), so a capture can name the state it saw. Until
        # then each merge below refreshes the snapshot itself.
        self._subscription = None
        self._updates = self.doc.observe(self._transaction_changed)

    @classmethod
    async def open(cls, path: Path, state_path: Path, *, backlog_limit=MAX_TEXT_BYTES):
        data = await asyncio.to_thread(path.read_bytes)
        store = await asyncio.to_thread(LiveStore, state_path)
        initial = create(data.decode("utf-8"))
        await asyncio.to_thread(store.initialize, initial.get_update(), file_hash(data))
        durable = create("")
        for update in await asyncio.to_thread(store.load_updates):
            durable.apply_update(update)
        live = cls(path, store, clone(durable.get_update()), durable, backlog_limit)
        live.on_disk = (file_hash(data), data.decode("utf-8"))
        mirror = await asyncio.to_thread(store.mirror)
        live.bases[mirror.file_hash] = mirror.state
        return live

    @property
    def text(self) -> str:
        return str(self._text)

    @property
    def digest(self) -> str:
        return text_digest(self.text)

    def _changed(self, event) -> None:
        # One pass over the text before the edit; ranges, reservations and the past states all follow these steps.
        steps = utf16_delta(self._before, event.delta)
        ids = list(self.ranges)
        spans, opened = follow_steps(list(self.ranges.values()), steps,
                                     {index for index, identifier in enumerate(ids) if identifier in self.open_ends})
        self.ranges.update(zip(ids, spans))
        self.open_ends = {ids[index] for index in opened}
        held = list(self._reservations.values())
        for reservation, span in zip(held, shift_spans([item.span for item in held], steps)):
            reservation.span = span
        if self.states:
            self.states.replaced(steps)
        self._before = self.text
        self.snapshot = (self._before, dict(self.ranges))
        self.text_changed()

    def _merged_text(self) -> None:
        if self._subscription is None:
            self._before = self.text
            self.snapshot = (self._before, dict(self.ranges))
            self.text_changed()
        if self.states:  # read after the transaction, when the state can be read
            self.states.stamp(self._current_key())

    def _current_key(self) -> StateKey:
        text = self.text
        return state_key(self.doc.get_state(), utf16_length(text), text_digest(text))

    def _transaction_changed(self, event) -> None:
        # Document state cannot be read inside the transaction; ``_apply``
        # announces the update as soon as the transaction ends.
        self._merged.append((event.update, self._sender, self._author))

    def _apply(self, update: bytes, sender=None, author: dict | None = None) -> None:
        """Merge one update and announce it before returning, in merge order.

        ``author`` names the agent behind a live write; users, sync and recovery omit it.
        """
        self._sender, self._author = sender, author
        try:
            self.doc.apply_update(update)
        finally:
            self._sender, self._author = None, None
            self._merged_text()
            merged, self._merged = self._merged, []
            # Each update keeps the author it merged with; one queued by
            # ``apply_unsaved`` is never credited to this call's writer.
            for change, origin, writer in merged:
                self.update_changed(change, origin, writer)

    def apply_unsaved(self, update: bytes) -> None:
        """Merge a landed candidate the store refused; announced with the next merge."""
        self.doc.apply_update(update)
        self._merged_text()

    def fits(self, start: int, end: int) -> bool:
        """Whether UTF-16 offsets lie inside the live text on character boundaries."""
        data = self.text.encode("utf-16-le")
        if not 0 <= start <= end <= len(data) // 2:
            return False
        try:
            data[:start * 2].decode("utf-16-le")
            data[:end * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            return False
        return True

    def _observe(self, start: int, end: int) -> None:
        if not self.fits(start, end):
            raise ValueError("Comment range is outside the live document")
        self._subscribe()

    def _subscribe(self) -> None:
        """Follow every edit from now on: tracked ranges move with it and the recent states start."""
        if self._subscription is None:
            self._before = self.text
            self._subscription = self._text.observe(self._changed)
            self.states.start(self._current_key())

    def track_range(self, identifier: str, start: int, end: int) -> None:
        self._observe(start, end)
        if self.ranges.get(identifier) != (start, end):
            self.open_ends.discard(identifier)
        self.ranges[identifier] = (start, end)
        self.snapshot = (self.text, dict(self.ranges))

    def reserved_by(self, identifier: str, user_id: str) -> Reservation | None:
        """The selection this user's capture tracks under ``identifier``, if any; another user's id is refused."""
        held = self._reservations.get(identifier)
        if held is not None and held.user_id != user_id:
            raise IdentityInUse("Comment identity is already in use")
        return held

    def reserve(self, identifier: str, start: int, end: int, user_id: str, holder: object, *,
                vector: bytes, digest: str, length: int, quote: str | None = None) -> bool:
        """Track a captured selection under its comment's client id, made in the live state or a recent one.

        The browser's state is its clocks, length and digest (``state_key``). A recent state needs the
        selection's quote, and the selection mapped to now must still hold exactly that text.
        False means the state is unknown or the selected text changed, and nothing was tracked.
        """
        self._subscribe()
        asked = state_key(vector, length, digest)
        span = self.states.span_now(asked, self._current_key(), self.text, start, end, quote)
        if span is None:
            return False
        self.reserved_by(identifier, user_id)
        if identifier in self.ranges:
            raise IdentityInUse("Comment identity is already in use")
        self._observe(*span)
        self._reservations.pop(identifier, None)
        self._reservations[identifier] = Reservation(user_id, holder, span)
        mine = [key for key, held in self._reservations.items() if held.holder is holder]
        for key in mine[:max(0, len(mine) - RESERVATION_LIMIT)]:
            del self._reservations[key]
        return True

    def held_span(self, identifier: str) -> tuple[int, int] | None:
        """Where the live text now holds the selection reserved under ``identifier``."""
        held = self._reservations.get(identifier)
        return held.span if held is not None else None

    def adopt(self, identifier: str, start: int, end: int) -> None:
        """Turn the reservation held under ``identifier`` into the comment's own tracked range, as one step."""
        self._reservations.pop(identifier, None)
        self.track_range(identifier, start, end)

    def release(self, identifier: str) -> None:
        self._reservations.pop(identifier, None)

    def release_held(self, identifier: str, holder: object) -> None:
        held = self._reservations.get(identifier)
        if held is not None and held.holder is holder:
            del self._reservations[identifier]

    def release_holder(self, holder: object) -> None:
        for identifier in tuple(self._reservations):
            self.release_held(identifier, holder)

    def range_text(self, identifier: str) -> str:
        start, end = self.ranges[identifier]
        return self.text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")

    def receive(self, message: BrowserMessage, *, sender=None) -> bool:
        prior = self.received.get(message.message_id)
        if prior is not None:
            if prior.update != message.update:
                raise ValueError("A message id cannot name two document updates")
            return True
        if self.backlog_bytes + len(message.update) > self.backlog_limit:
            return False
        # An update cannot add more text bytes than it has, so only a bound
        # over the limit needs the exact check.
        if len(self.snapshot[0].encode("utf-8")) + len(message.update) > MAX_TEXT_BYTES:
            candidate = clone(self.doc.get_update())
            candidate.apply_update(message.update)
            if len(str(candidate["text"]).encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("Document is too large")
        self._apply(message.update, sender)
        self.received[message.message_id] = message
        self.backlog_bytes += len(message.update)
        return True

    def received_snapshot(self) -> tuple[BrowserMessage, ...]:
        return tuple(self.received.values())

    def committed(self, messages: Sequence[BrowserMessage], ack: tuple[str, ...]) -> None:
        """Called only after the store transaction lands, still under its lock."""
        for message in messages:
            self.durable.apply_update(message.update)
        count = len(messages)
        if tuple(islice(self.received.values(), count)) != tuple(messages):
            raise RuntimeError("Document batch arrival order changed during commit")
        for message in messages:
            del self.received[message.message_id]
        self.backlog_bytes -= sum(len(message.update) for message in messages)
        if ack:
            self.acknowledge(ack)

    async def commit_received(self) -> tuple[str, ...]:
        async with self.committer_lock:
            messages = self.received_snapshot()
            if not messages:
                return ()
            ack = await asyncio.to_thread(self.store.commit_batch, messages)
            self.committed(messages, ack)
            return ack

    async def apply_committed(self, update: bytes, state: bytes, result_hash: str,
                               author: dict | None = None) -> None:
        self.durable.apply_update(update)
        # The store has promoted this delta already; live follows it after
        # one loop pass, so due timers and the tasks they wake run first.
        await yield_loop_pass()
        self._apply(update, author=author)
        self.bases.pop(result_hash, None)
        self.bases[result_hash] = state
        while len(self.bases) > self.base_limit:
            self.bases.pop(next(iter(self.bases)))

    def close(self) -> None:
        if self._subscription is not None:
            self._text.unobserve(self._subscription)
            self._subscription = None
        if self._updates is not None:
            self.doc.unobserve(self._updates)
            self._updates = None
        self.text_changed = lambda: None
        self.update_changed = lambda update, sender, author: None
        self.preview_changed = lambda frame: None
        self.failure_changed = lambda kind, message, provenance: None
        self.pending_changed = lambda: None
        self.read_bases.clear()
        self.sections.clear()
        self._reservations.clear()
        self.states.clear()
        self.acknowledge = lambda ids: None
        self.viewer_actor = lambda: None
        self.store.observe_refusal = lambda error: None
