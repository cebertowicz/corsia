"""Codex rollout file identity, discovery, and isolated copies.

A Codex thread's provider session is one rollout file at
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` whose first line
is ``{"type":"session_meta","payload":{"id":"<uuid>",...}}``. For CORSIA
threads that home is the managed app-server home under the runtime dir, not
the user's ``~/.codex``.

Checkpoint work copies the exact rollout into a disposable Codex home and
resumes it there through ``core.checkpoint_codex_fork``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from llm.isolated_structured import (
    IsolatedStructuredError,
    _is_canonical_uuid_text,
    _strict_json_loads,
)


_ROLLOUT_SUFFIX = ".jsonl"
# How far back the date-partitioned rollout walk looks. Rollouts accumulate
# indefinitely, so the search must stay bounded by recency rather than total
# history; a thread idle longer than this is a cold-path candidate anyway.
CODEX_ROLLOUT_SEARCH_HORIZON_DAYS = 30
# The cumulative fields a resume restores; derived totals are not billed again.
_RESTORED_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")


def default_codex_sessions_root() -> Path:
    """The managed Codex app-server home's rollout directory.

    CORSIA's Codex threads run in an isolated ``CODEX_HOME`` (the app-server
    home under the runtime dir), so their rollouts live there — not in the
    user's ``~/.codex``.
    """
    from subagents.codex_env import _default_app_server_home

    return _default_app_server_home() / "sessions"


def _numeric_dirs_desc(parent: Path) -> list[tuple[int, Path]]:
    """Child directories with numeric names, newest first."""
    try:
        entries = [
            (int(entry.name), entry)
            for entry in parent.iterdir()
            if entry.is_dir() and entry.name.isdigit()
        ]
    except OSError:
        return []
    entries.sort(key=lambda item: item[0], reverse=True)
    return entries


def _day_matches(
    day_dir: Path,
    provider_thread_id: str,
) -> Path | None:
    matches = [
        path
        for path in day_dir.glob(
            f"rollout-*-{provider_thread_id}{_ROLLOUT_SUFFIX}"
        )
        if path.is_file() and not path.is_symlink()
    ]
    if not matches:
        return None
    # One thread has one rollout; if the layout ever produces more in a day,
    # take the newest so the fork resumes current state, not a stale copy.
    return max(matches, key=lambda path: path.stat().st_mtime_ns)


def find_codex_rollout(
    provider_thread_id: str,
    *,
    sessions_root: Path,
    horizon_days: int = CODEX_ROLLOUT_SEARCH_HORIZON_DAYS,
) -> Path | None:
    """Locate the rollout carrying this thread uuid, newest day first.

    The sessions tree is date-partitioned (``YYYY/MM/DD``), so the walk visits
    day directories newest-first, exits on the first match, and never descends
    past ``horizon_days`` — the cost is bounded by the horizon, not by total
    historical session volume. ``None`` therefore also means "no rollout
    within the search horizon".
    """
    if not _is_canonical_uuid_text(provider_thread_id):
        return None
    oldest_allowed = (
        datetime.datetime.now(datetime.timezone.utc).date()
        - datetime.timedelta(days=horizon_days)
    )
    try:
        for year, year_dir in _numeric_dirs_desc(sessions_root):
            if year < oldest_allowed.year:
                return None
            for month, month_dir in _numeric_dirs_desc(year_dir):
                for day, day_dir in _numeric_dirs_desc(month_dir):
                    try:
                        day_date = datetime.date(year, month, day)
                    except ValueError:
                        continue
                    if day_date < oldest_allowed:
                        # Directories are visited newest-first, so everything
                        # after this day is older still.
                        return None
                    match = _day_matches(day_dir, provider_thread_id)
                    if match is not None:
                        return match
    except OSError:
        return None
    return None


def registered_rollout_candidate(
    recorded_path: str | None,
    *,
    sessions_root: Path,
) -> Path | None:
    """A durably recorded rollout path, when it still structurally checks out.

    The registry record is a location cache, never identity authority: the
    candidate must be an existing regular non-symlink file inside
    ``sessions_root`` (matching the trust bar of the bounded walk). Callers
    must still prove identity from the file's ``session_meta`` header before
    using it; any failure here means "fall back to the walk".
    """
    if not recorded_path:
        return None
    candidate = Path(recorded_path)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(sessions_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    if candidate.is_symlink() or not resolved.is_file():
        return None
    return candidate


def rollout_sha256(path: Path) -> str:
    """Streamed sha256 of one rollout file."""
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_codex_rollout_prefix(path: Path, byte_length: int | None = None) -> bytes:
    """Read the current rollout or one previously frozen exact prefix."""
    if byte_length is not None and byte_length <= 0:
        raise IsolatedStructuredError("Codex rollout prefix length is invalid")
    with open(path, "rb") as stream:
        prefix = stream.read() if byte_length is None else stream.read(byte_length)
    if byte_length is not None and len(prefix) != byte_length:
        raise IsolatedStructuredError("Codex rollout frozen prefix was truncated")
    if not prefix:
        raise IsolatedStructuredError("Codex rollout is empty")
    return prefix


def _session_meta_from_prefix(prefix: bytes) -> dict[str, Any]:
    header_line = prefix.partition(b"\n")[0]
    try:
        header = _strict_json_loads(header_line)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IsolatedStructuredError(
            "Codex rollout header is not strict JSON"
        ) from exc
    payload = header.get("payload") if isinstance(header, dict) else None
    if (
        not isinstance(header, dict)
        or header.get("type") != "session_meta"
        or not isinstance(payload, dict)
        or not _is_canonical_uuid_text(payload.get("id"))
    ):
        raise IsolatedStructuredError(
            "Codex rollout does not start with a valid session_meta record"
        )
    return payload



def _restored_token_totals(line: bytes) -> dict[str, int] | None:
    """The running totals one rollout ``token_count`` line carries, if any."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    info = payload.get("info") if isinstance(payload, dict) else None
    total = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(total, dict):
        return None
    if any(
        type(total.get(field)) is not int or total[field] < 0
        for field in _RESTORED_TOKEN_FIELDS
    ):
        raise IsolatedStructuredError(
            "Codex rollout token_count has no complete cumulative usage receipt"
        )
    return {field: total[field] for field in _RESTORED_TOKEN_FIELDS}


def codex_rollout_prefix_proof(
    path: Path,
    byte_length: int | None = None,
) -> tuple[int, str, dict[str, Any], dict[str, int]]:
    """Freeze length, digest, header, and restored usage from the same bytes."""
    if byte_length is not None and byte_length <= 0:
        raise IsolatedStructuredError("Codex rollout prefix length is invalid")
    restored: dict[str, int] = {}
    digest = hashlib.sha256()
    size = 0
    remaining = byte_length
    header: dict[str, Any] | None = None
    with open(path, "rb") as stream:
        while remaining is None or remaining:
            line = stream.readline(remaining)
            if not line:
                break
            digest.update(line)
            size += len(line)
            if remaining is not None:
                remaining -= len(line)
            if header is None:
                header = _session_meta_from_prefix(line)
            totals = _restored_token_totals(line)
            if totals is not None:
                restored = totals
    if remaining:
        raise IsolatedStructuredError("Codex rollout frozen prefix was truncated")
    if header is None:
        raise IsolatedStructuredError("Codex rollout is empty")
    return size, digest.hexdigest(), header, restored


def copy_codex_rollout_to_isolated_home(
    source: Path,
    *,
    source_thread_id: str,
    source_sessions_root: Path,
    isolated_home: Path,
    source_prefix_bytes: int,
) -> Path:
    """Copy one frozen prefix at its source-relative sessions path."""
    try:
        resolved_source = source.resolve(strict=True)
        relative_path = resolved_source.relative_to(
            source_sessions_root.resolve(strict=True)
        )
    except (OSError, ValueError) as exc:
        raise IsolatedStructuredError(
            "Codex rollout source escaped its sessions root"
        ) from exc
    if source.is_symlink() or not resolved_source.is_file():
        raise IsolatedStructuredError("Codex rollout source path was unsafe")
    prefix = read_codex_rollout_prefix(source, source_prefix_bytes)
    if _session_meta_from_prefix(prefix).get("id") != source_thread_id:
        raise IsolatedStructuredError(
            "Codex rollout header does not carry the expected source identity"
        )
    copy_path = isolated_home / "sessions" / relative_path
    copy_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if copy_path.exists():
        raise IsolatedStructuredError(
            "Codex isolated rollout copy already exists"
        )
    with open(copy_path, "xb") as stream:
        stream.write(prefix)
    return copy_path
