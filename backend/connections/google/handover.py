"""Move the former Assistant's Google sign-in into CORSIA without dual token stores."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from automation import schedules
from connections import gws_agent_home
from connections.google import oauth
from core import thread_lifecycle
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso
from tools.corsia_api import conductor_user

LEGACY_ASSISTANT_RESPONSIBILITY = "Email, calendar, Drive, and Docs assistance"
LEGACY_ASSISTANT_DOMAINS = ["email", "calendar", "drive", "docs"]
LEGACY_ASSISTANT_NOTES = "Created through Assistant onboarding."

MISSING_ASSISTANT = "No eligible old Assistant Google sign-in was found. Nothing was moved; use google_connect to sign in."
AMBIGUOUS_ASSISTANT = "More than one eligible old Assistant was found. Nothing was moved; CORSIA will not choose one. Archive or remove the unwanted Assistant, then retry."
MISSING_CLIENT = "The old Assistant has no Desktop OAuth client file to move. Nothing was moved; use google_connect with the person's downloaded Desktop app client JSON."
ALREADY_CONNECTED = "Google is already connected to CORSIA. No Assistant handover was made."
PENDING = "A CORSIA Google sign-in is already waiting for the person's Allow click. Complete it or wait up to 10 minutes, then retry; the Assistant was not changed."
CLEANUP_FAILED = "Couldn't finish moving the old Assistant Google sign-in. No CORSIA sign-in was saved; say \"take over my email\" again."


class HandoverError(oauth.GoogleSignInError):
    """A fixed, person-safe handover refusal."""


class _Assistant:
    def __init__(self, thread_id: str, home: Path) -> None:
        self.thread_id = thread_id
        self.home = home


def _legacy_candidates(user_id: str) -> list[str]:
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT id,agent_domains FROM threads WHERE user_id=? AND title='Assistant' "
            "AND COALESCE(agent_enabled,0)=1 AND agent_responsibility=? AND agent_notes=? "
            "AND COALESCE(lifecycle_state,'active') != 'purged'",
            (user_id, LEGACY_ASSISTANT_RESPONSIBILITY, LEGACY_ASSISTANT_NOTES),
        ).fetchall()
        candidates: list[str] = []
        for row in rows:
            try:
                domains = json.loads(row["agent_domains"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if domains == LEGACY_ASSISTANT_DOMAINS:
                candidates.append(str(row["id"]))
        return candidates
    finally:
        db.close()


def _select_assistant(user_id: str) -> _Assistant:
    candidates = _legacy_candidates(user_id)
    if not candidates:
        raise HandoverError(MISSING_ASSISTANT)
    if len(candidates) != 1:
        raise HandoverError(AMBIGUOUS_ASSISTANT)
    home = gws_agent_home.existing_home_for(candidates[0])
    if home is None:
        raise HandoverError(MISSING_ASSISTANT)
    return _Assistant(candidates[0], home)


def _schedule_step(db, thread_id: str) -> None:
    # Every open schedule goes: an archived thread's schedules would still fire.
    schedules.retire_for_thread(thread_id, utc_now_iso(), db=db)


def _archive_step(db, thread_id: str) -> None:
    thread_lifecycle.set_user_archive_state(db, thread_id, archived=True)


def _home_step(home: Path) -> None:
    if not home.exists():
        return
    if home.is_symlink() or not home.is_dir():
        raise HandoverError(CLEANUP_FAILED)
    shutil.rmtree(home)


def _complete_before_store(assistant: _Assistant) -> None:
    """Commit old-Assistant retirement only when its credential home is gone.

    The database transaction rolls schedule cancellation and archiving back if
    removal fails. The OAuth refresh token is still held only by the caller.
    """
    try:
        with write_transaction(None, scope="google_handover_cleanup") as db:
            _schedule_step(db, assistant.thread_id)
            _archive_step(db, assistant.thread_id)
            _home_step(assistant.home)
    except Exception:
        raise HandoverError(CLEANUP_FAILED) from None


async def start(actor_thread_id: str) -> str:
    """Start one person-authorized adoption; callback cleanup runs before token storage."""
    if oauth.connected_email() is not None:
        raise HandoverError(ALREADY_CONNECTED)
    if oauth.sign_in_pending():
        raise HandoverError(PENDING)
    user_id = await run_in_db(conductor_user, actor_thread_id)
    assistant = await run_in_db(_select_assistant, user_id)
    client_file = assistant.home / "config" / "desktop-client.json"
    if client_file.is_file():
        await oauth.import_client_file(str(client_file))
    elif not await run_in_db(oauth.has_stored_client):
        raise HandoverError(MISSING_CLIENT)

    async def after_exchange() -> None:
        await run_in_db(_complete_before_store, assistant)

    return await oauth.start_sign_in(
        after_exchange=after_exchange,
        store_failure_text=CLEANUP_FAILED,
    )
