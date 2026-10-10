"""Bounded read-only shared decisions and independent live identity setup."""
from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
from typing import TYPE_CHECKING

from doceditor.document_save import DocumentSaveError
from doceditor.live_document import LiveDocument
from doceditor.live_publication_record import Publication, WriteActor
from registry.shared_documents import resolve_shared_target
from registry.document_ownership import document_write_authority
from registry.shared_write_errors import DurableWritePolicyError, SharedIdentitySetupRequired


if TYPE_CHECKING:
    from doceditor.live_runtime import LiveDocuments


class SharedLivePolicy:
    """Prepare publication evidence using the canonical policy off the event loop."""

    def __init__(self, live: LiveDocument, *, owner: LiveDocuments):
        self.live = live
        self.owner = owner
        self.workspace = owner.workspace
        self.db_path = owner.db_path
        self.schedule_setup = None

    async def decide(self, operation, text: str) -> Publication | None:
        """Return immutable evidence, or refuse and enqueue setup without joining it."""
        actor = operation.actor
        if actor is None:
            raise ValueError("Live shared writes require a server-issued actor")
        self._check_failure()
        mirror = await asyncio.to_thread(self.live.store.mirror)
        try:
            return await asyncio.to_thread(self.decision, actor, text, mirror)
        except SharedIdentitySetupRequired:
            if self.schedule_setup is not None:
                self.schedule_setup(lambda: self.setup(actor))
            raise

    async def admits(self, actor: WriteActor) -> bool:
        """Whether ``actor`` may write here now; a refusal or a write waiting on setup is no. Read-only."""
        try:
            self._check_failure()
            mirror = await asyncio.to_thread(self.live.store.mirror)
            await asyncio.to_thread(self.decision, actor, None, mirror)
        except (DocumentSaveError, DurableWritePolicyError):  # setup-required is one of these
            return False
        return True

    def decision(self, actor: WriteActor, text: str | None, mirror) -> Publication | None:
        """Blocking, read-only: the publication ``text`` gets against ``mirror``, the file it replaces.

        Without ``text`` the decision is made before the content is known, so no edit counts as mechanical.
        """
        with closing(sqlite3.connect(self.db_path, timeout=1, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=1000")
            shared = resolve_shared_target(
                self.live.path, workspace=self.workspace, thread_id=actor.thread_id,
                project_id=actor.project_id, direct_user=actor.direct_user, description=actor.description,
                proposed_content=None if text is None else text.encode("utf-8"), setup=False, promoted_hash=mirror.file_hash, db=db,
            )
            authority = None if shared is not None else document_write_authority(
                self.live.path, workspace=self.workspace, thread_id=actor.thread_id,
                direct_user=actor.direct_user, db=db,
            )
        decision = Publication.decision_for(self.live.path, shared, authority)
        return Publication(actor.thread_id, actor.origin, actor.description, mirror.state, decision)

    def _check_failure(self) -> None:
        failure, _ = self.owner.setup_state(self.live.path)
        if failure:
            raise DocumentSaveError("setup_failed", failure.reason)

    async def setup(self, actor: WriteActor) -> None:
        """Run the common setup in an independent lane item; any success clears failure."""
        try:
            await asyncio.to_thread(self.owner.setup_shared, self.live.path, actor)
        except Exception as error:
            raise DocumentSaveError("setup_failed", str(error)) from error
