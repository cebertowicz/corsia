"""Render and atomically install Claude Code resume transcripts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from history.types import PortableHistoryProjection
from subagents.cc_context_types import CCPreparedTranscript, CCTranscriptReconstruction
from subagents.cc_transcript_projection import message_timestamp, project_cc_messages


_RECONSTRUCTED_VERSION = "corsia-reconstructed"


def _transcript_root(path: str) -> Path:
    return Path(path).parents[2]


def _json_object(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _version_from_transcript(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for _, line in zip(range(40), stream):
                data = _json_object(line)
                version = str((data or {}).get("version") or "").strip()
                if version:
                    return version
    except OSError:
        return None
    return None


def _latest_transcript_version(root: Path) -> str:
    projects = root / "projects"
    if not projects.is_dir():
        return _RECONSTRUCTED_VERSION
    try:
        files = sorted(
            projects.glob("*/*.jsonl"), key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return _RECONSTRUCTED_VERSION
    for path in files[:50]:
        version = _version_from_transcript(path)
        if version:
            return version
    return _RECONSTRUCTED_VERSION


def _claude_row(
    *,
    row_type: str,
    message: dict[str, Any],
    cc_session_id: str,
    cwd: str,
    row_uuid: str,
    parent_uuid: str | None,
    timestamp: str,
    version: str,
) -> dict[str, Any]:
    return {
        "type": row_type, "message": message, "cwd": cwd,
        "sessionId": cc_session_id, "uuid": row_uuid,
        "parentUuid": parent_uuid, "isSidechain": False,
        "userType": "external", "timestamp": timestamp,
        "version": version, "gitBranch": "",
    }


def _history_entry_exists(path: Path, cc_session_id: str, project: str) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                data = _json_object(line)
                if data and (
                    data.get("sessionId") == cc_session_id
                    and data.get("project") == project
                ):
                    return True
    except OSError:
        return False
    return False


def _timestamp_millis(timestamp: str) -> int:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _ensure_history_entry(
    *, root: Path, cc_session_id: str, project: str,
    display: str, timestamp: str,
) -> None:
    history_path = root / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    if _history_entry_exists(history_path, cc_session_id, project):
        return
    entry = {
        "display": display if display.strip() else "CORSIA reconstructed session",
        "pastedContents": {}, "timestamp": _timestamp_millis(timestamp),
        "project": project, "sessionId": cc_session_id,
    }
    with history_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")


def _transcript_path_for_cwd(
    *, claude_dir: str, working_dir: str | None, cc_session_id: str
) -> str:
    from subagents.cc_sessions import resolve_claude_dir

    if not claude_dir:
        # The launch resumes from its thread's own root; a guessed Default here
        # strands the transcript where an added account's launch cannot see it.
        raise ValueError("Claude Code transcript requires its thread's Claude root")
    root = Path(resolve_claude_dir(claude_dir))
    cwd = os.path.abspath(working_dir or os.getcwd())
    return str(root / "projects" / cwd.replace(os.sep, "-") / f"{cc_session_id}.jsonl")


def _render_rows(projected_messages, *, cc_session_id: str, cwd: str, version: str):
    lines: list[str] = []
    parent_uuid: str | None = None
    first_user_display = ""
    first_timestamp = ""
    for projected in projected_messages:
        timestamp = message_timestamp(projected.source)
        row = _claude_row(
            row_type=projected.row_type, message=projected.message,
            cc_session_id=cc_session_id, cwd=cwd,
            row_uuid=projected.row_uuid, parent_uuid=parent_uuid,
            timestamp=timestamp, version=version,
        )
        lines.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
        parent_uuid = projected.row_uuid
        if projected.source.role == "user" and not first_user_display:
            first_user_display = projected.source.content.strip()
            first_timestamp = timestamp
    return lines, first_user_display, first_timestamp


def prepare_cc_transcript_from_corsia(
    *,
    projection: PortableHistoryProjection,
    cc_session_id: str,
    path: str,
    working_dir: str | None,
    destination_provider: str | None = None,
    destination_effective_model: str | None = None,
    allowed_corsia_tool_names: frozenset[str] = frozenset(),
    tool_result_budget_tokens: int | None = None,
    tool_argument_budget_tokens: int | None = None,
) -> CCPreparedTranscript:
    """Render one Claude resume transcript without mutating provider state."""
    cwd = working_dir or os.getcwd()
    root = _transcript_root(path)
    projected = project_cc_messages(
        projection, cc_session_id=cc_session_id,
        destination_provider=destination_provider,
        destination_effective_model=destination_effective_model,
        allowed_corsia_tool_names=allowed_corsia_tool_names,
        tool_result_budget_tokens=tool_result_budget_tokens,
        tool_argument_budget_tokens=tool_argument_budget_tokens,
    )
    lines, display, timestamp = _render_rows(
        projected, cc_session_id=cc_session_id, cwd=cwd,
        version=_latest_transcript_version(root),
    )
    content = "\n".join(lines) + ("\n" if lines else "")
    reconstruction = CCTranscriptReconstruction(
        path=str(Path(path)), cc_session_id=cc_session_id,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        line_count=len(lines), format_version=projection.format_version,
        start_exclusive=projection.start_exclusive,
        sealed_boundary=projection.sealed_boundary,
        full_raw_replay_available=projection.full_raw_replay_available,
        snapshot_history=tuple(item.message for item in projected),
    )
    return CCPreparedTranscript(
        reconstruction=reconstruction, content=content,
        transcript_root=str(root), project=cwd,
        display=display, timestamp=timestamp,
    )


def install_cc_prepared_transcript(
    prepared: CCPreparedTranscript,
) -> CCTranscriptReconstruction:
    """Install the exact resume bytes already measured by preflight."""
    target = Path(prepared.reconstruction.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.corsia-rebuild")
    temporary.write_text(prepared.content, encoding="utf-8")
    os.replace(temporary, target)
    _ensure_history_entry(
        root=Path(prepared.transcript_root),
        cc_session_id=prepared.reconstruction.cc_session_id,
        project=prepared.project, display=prepared.display,
        timestamp=prepared.timestamp,
    )
    return prepared.reconstruction


def reconstruct_cc_transcript_from_corsia(**kwargs) -> CCTranscriptReconstruction:
    """Render and install one canonical portable projection for Claude resume."""
    return install_cc_prepared_transcript(prepare_cc_transcript_from_corsia(**kwargs))
