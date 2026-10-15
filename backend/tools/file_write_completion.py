"""Complete a confirmed file save with its optional context-source notice.

One owner records the passive context notice for every supported save surface:
the agent file tools wrap a string result here, and the document-editor API
attaches the same failure text to its JSON receipt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from infra.guidance_git import digest, guidance_target as guidance_target, save_guidance
from infra.guidance_messages import saver_author


_NOTICE_FAILURE = (
    "File saved. The context-change notice could not be recorded; other agents "
    "may still use earlier instructions."
)
_log = logging.getLogger(__name__)
_GUIDANCE_ONLY = "For skills, bootloaders and context files only: "
GUIDANCE_PARAMETERS = {
    "reason": {"type": "string", "description": _GUIDANCE_ONLY + "one line on why, naming the decision that changes (not the incident)."},
    "evidence": {"type": "string", "description": _GUIDANCE_ONLY + "identifiers or URLs that back the reason (thread, message, artifact, issue)."},
}


def record_context_source_change(
    path: Path,
    *,
    thread_id: str | None,
    workspace: Path,
) -> str | None:
    """Journal a saved context source; return the visible failure text or None.

    A non-source path deliberately journals nothing and returns None. A
    classifier or journal failure returns the partial-result text: the bytes are
    already saved, but other agents might not receive the passive notice. The
    caller keeps its primary save outcome either way; nothing here retries.
    """
    if not thread_id:
        return None
    try:
        from core.context_source_paths import append_context_source_change

        append_context_source_change(
            None,
            path=str(path),
            workspace=workspace,
            actor_thread_id=thread_id,
        )
    except Exception:
        _log.warning(
            "context-source notice failed for saved file %s",
            path,
            exc_info=True,
        )
        return _NOTICE_FAILURE
    return None


def complete_file_write(
    path: Path,
    result: str,
    *,
    thread_id: str | None,
    workspace: Path,
) -> str:
    """Attach the notice outcome to an agent file tool's string result."""
    failure = record_context_source_change(
        path, thread_id=thread_id, workspace=workspace
    )
    if failure is None:
        return result
    return _with_line(result, "context_notice_error", _NOTICE_FAILURE)


def _with_line(result: str, key: str, line: str) -> str:
    """Keep managed receipts parseable while attaching a visible outcome line."""
    try:
        receipt = json.loads(result)
    except (TypeError, ValueError):
        receipt = None
    if isinstance(receipt, dict) and receipt.get("registry_managed") is True:
        receipt[key] = line
        return json.dumps(receipt, sort_keys=True)
    return f"{result}\n\n{line}"


def managed_write(
    path: Path, text: str, kwargs: dict, *, reviewed: bool, producer: str, success: str,
    description: str | None = None, owner: str | None = None,
    update_trigger: str | None = None, handle=None, notify: bool = True,
) -> str:
    """The agent file tools' one write: through the registry when a thread owns it, else in
    place through ``handle`` or a bound descriptor; then the context notice. Returns the result.

    ``notify=False`` defers the registry's socket nudge to the caller, which must
    fire it on the event loop with :func:`announce_published_write`; a caller that
    runs this off the loop (where the broadcast layer cannot schedule) needs it."""
    from infra.config import WORKSPACE
    from registry.durable_writes import write_text
    from registry.registration_writer import origin_from_tool_context
    from tools.filesystem_effects import write_bound_text

    thread_id = kwargs.get("_thread_id")
    decision = write_text(
        path, text, thread_id=thread_id, project_id=kwargs.get("_thread_project_id"), workspace=WORKSPACE,
        description=description, owner=owner, update_trigger=update_trigger,
        reviewed=reviewed, origin=origin_from_tool_context(thread_id, kwargs, producer),
        notify=notify,
    ) if thread_id else None
    reported = None
    if isinstance(decision, dict):
        success = json.dumps(decision, sort_keys=True)
    else:
        # An ordinary path: the registry decided ownership before this write and the owner hears of it after.
        before = path.read_bytes() if decision is not None and decision.reaches_owner else None
        if handle is not None:
            handle.seek(0)
            handle.write(text)
            handle.truncate()
            handle.flush()
        else:
            write_bound_text(path, text)
        reported = (decision, before)
    result = complete_file_write(path, success, thread_id=thread_id, workspace=WORKSPACE) if thread_id else success
    if reported is not None:
        _report_plain_write(reported[0], path, reported[1], thread_id)
    return result


def _report_plain_write(decision, path: Path, before: bytes | None, thread_id: str | None) -> None:
    """Report a landed ordinary write to the document's owner under the decision made before it."""
    if decision is None or not decision.reaches_owner or not thread_id:
        return
    from infra.database import get_db
    from registry.shared_document_events import report_plain_write

    db = get_db()
    try:
        # The bytes are saved and the notice recorded: a report failure never fails the write.
        report_plain_write(decision, path, before, thread_id=thread_id, db=db)
    except Exception as exc:
        _log.warning("owner report failed for saved artifact %s: %s", decision.artifact_id, type(exc).__name__)
    finally:
        db.close()


def announce_published_write(result: str) -> str:
    """Fire the deferred artifact nudge for a governed write from the caller's loop.

    The registry broadcast layer schedules onto the running event loop, so a write
    that ran off it (``notify=False``) cannot nudge an open tab itself. This
    caller-side seam fires that one nudge on the loop, after the committed write,
    exactly as the registry did before the write moved off the loop. A failed or
    ungoverned result carries no artifact and nudges nothing.
    """
    from registry import service

    try:
        receipt = json.loads(result)
    except (TypeError, ValueError):
        return result
    if isinstance(receipt, dict) and receipt.get("registry_managed") is True:
        artifact = receipt.get("artifact")
        if artifact:
            service.notify_changed(artifact)
    return result


def save_guided_write(
    path: Path,
    text: str,
    target,
    kwargs: dict,
    expected: str | None = None,
    **write_args,
) -> str:
    """Blocking. Save an agent tool's guidance write through the history primitive."""
    from infra.config import WORKSPACE

    results: list[str] = []
    saved = save_guidance(
        {path: text.encode()}, workspace=WORKSPACE, target=target,
        author=saver_author(thread_id=kwargs.get("_thread_id")), reason=kwargs.get("reason"),
        evidence=kwargs.get("evidence"), expected=None if expected is None else {path: digest(expected)},
        write=lambda target_path, data: results.append(
            managed_write(target_path, data.decode(), kwargs, **write_args)
        ),
    )
    receipt = results[0] if results else None
    result = (
        _with_line(receipt, "guidance_history", saved.line)
        if receipt is not None and saved.line
        else receipt or saved.line or ""
    )
    return result


async def save_guided_write_and_announce(path: Path, text: str, target, kwargs: dict, **write_args) -> str:
    """Save guidance off-loop, then nudge its managed artifact from the caller loop."""
    from infra.db_async import run_off_db

    result = await run_off_db(
        save_guided_write, path, text, target, kwargs,
        notify=False, **write_args,
    )
    announce_published_write(result)
    return result
