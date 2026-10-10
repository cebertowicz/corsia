"""Shared identity setup delegates to the registry; failure truth stays in its runtime."""
from __future__ import annotations

from contextlib import closing, nullcontext
import logging
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from doceditor.live_publication_record import WriteActor
from registry import shared_documents
from registry.shared_write_errors import DurableWritePolicyError

if TYPE_CHECKING:
    from doceditor.live_runtime import LiveDocuments

log = logging.getLogger(__name__)


def setup_shared(owner: LiveDocuments, path: Path, actor: WriteActor, *, db=None) -> None:
    """The common open/write/Retry setup, always outside a started write's join."""
    try:
        connection = nullcontext(db) if db is not None else closing(
            sqlite3.connect(owner.db_path, timeout=5, isolation_level=None)
        )
        with connection as database:
            database.row_factory = sqlite3.Row
            shared_documents.resolve_shared_target(
                path, workspace=owner.workspace, thread_id=actor.thread_id,
                project_id=actor.project_id, direct_user=actor.direct_user,
                description=actor.description, db=database,
            )
    except DurableWritePolicyError:
        # A policy refusal is a decision, not a failed setup.
        raise
    except Exception as error:
        owner.setup_changed(path, str(error))
        log.warning("Document setup_failed transition remains owed: %s", error)
        from infra.product_events import emit_product_event
        emit_product_event("documents", "setup_failed", properties={"reason": str(error)})
        raise
    owner.setup_changed(path, "")
