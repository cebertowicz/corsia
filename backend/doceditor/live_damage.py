"""Save a live session's text once when its store is found damaged mid-session (design 5.7(a)).

The store's bytes are never touched. The save is a lane item, so it never runs
beside a lane write, and it goes through the lane's plain writer with the hash
of the file this session last wrote or imported: a file changed outside since
then is never replaced. It is attributed as the last-viewer flush is, to a
current writable viewer's actor.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from typing import TYPE_CHECKING

from doceditor import live_events
from doceditor.document_save import FileChangedOutside
from doceditor.live_admission import falls_back_to_plain, store_refusal
from doceditor.live_lane import LaneRefused
from doceditor.live_publication_record import Publication, WriteActor
from doceditor.live_publisher import register_publication
from doceditor.live_store import Mirror, StoreRefused
from doceditor.live_text import create
from infra.database import get_db

if TYPE_CHECKING:
    from doceditor.live_runtime import LiveEntry

log = logging.getLogger(__name__)


class DamageSave:
    """One owner per open document: at most one file write per session."""

    def __init__(self, entry: LiveEntry):
        self.entry = entry
        self.loop = asyncio.get_running_loop()
        self.scheduled = False
        self.attempted = False

    def observe(self, error: StoreRefused) -> None:
        """Store hook, called from any thread before a refusal is raised."""
        if not falls_back_to_plain(store_refusal(error).kind):
            return
        try:
            self.loop.call_soon_threadsafe(self.found, error)
        except RuntimeError:
            log.error("Live document %s found its store damaged after its loop closed; its text was not saved",
                      self.entry.artifact_id)

    def found(self, error: StoreRefused) -> None:
        """Queue the save behind admitted lane operations, once until it has run."""
        if self.attempted or self.scheduled:
            return
        self.scheduled = True
        # No older owed row can register after this save. A registration already
        # off-loop is joined inside save, before the new file/hash is published.
        self.entry.publisher.stop(error)
        try:
            self.entry.lane.schedule(self.save)
        except LaneRefused:
            self.scheduled = False
            self.attempted = True
            self._refused("lane_closed")

    async def save(self) -> None:
        self.scheduled = False
        await self.entry.publisher.close()
        live = self.entry.live
        prior_hash, prior_text = live.on_disk
        text = live.text
        if self.attempted or text == prior_text:
            return  # Nothing unsaved yet; a later damage finding may still save.
        self.attempted = True
        actor = live.viewer_actor()
        if actor is None:
            self._refused("no_viewer")
            return
        try:
            await self.entry.lane.validate_actor(text, actor)
            publication = await asyncio.to_thread(self._publication, actor, text, prior_text, prior_hash)
            await self.entry.lane.write(text, prior_hash)
        except FileChangedOutside:
            self._refused("file_changed_outside")
            return
        except Exception as error:
            self._refused("refused", error)
            return
        log.warning("Live document %s saved its text after its store was found damaged", self.entry.artifact_id)
        live_events.emit("damaged_text_saved", artifact_id=self.entry.artifact_id, store_id=live.store.store_id)
        try:
            await asyncio.to_thread(self._register, publication, text, live.on_disk[0])
        except Exception:
            log.exception("Live document %s saved after damage, but its registration failed", self.entry.artifact_id)

    def _publication(self, actor: WriteActor, text: str, prior_text: str, prior_hash: str) -> Publication:
        prior = Mirror(create(prior_text).get_update(), prior_hash)
        policy, path = self.entry.policy, self.entry.live.path
        publication = policy.decision(actor, text, prior) if policy is not None else None
        if publication is None:
            publication = Publication(actor.thread_id, actor.origin, actor.description, prior.state,
                                      Publication.decision_for(path, None))
        return replace(publication, ranges=dict(self.entry.live.ranges))

    @staticmethod
    def _register(publication: Publication, text: str, result_hash: str) -> None:
        db = get_db()
        try:
            register_publication(publication, text, result_hash, stamp=None, db=db)
        finally:
            db.close()

    def _refused(self, cause: str, error: Exception | None = None) -> None:
        log.error("Live document %s found its store damaged; its text was not saved (%s)%s",
                  self.entry.artifact_id, cause, f": {type(error).__name__}" if error else "")
        live_events.emit("damaged_save_refused", artifact_id=self.entry.artifact_id,
                         store_id=self.entry.live.store.store_id, cause=cause)
