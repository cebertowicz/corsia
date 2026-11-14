"""Whether a reviewed document went ahead as its person first saw it, read from the workspace history.

A review-gated run is clean only when the document's latest version at or before ``as_of`` holds the
same bytes as its version at or before ``first_round_at``. :func:`went_ahead_outcomes` answers a batch
with one database query and two Git calls, whatever the batch size; nothing new is stored.

Who wrote each version comes from what the history already records:

- ``accepted_suggestion``: the version carries the ``Suggestion-accept`` trailer, which only the accept
  paths write (``infra.guidance_messages.commit_message``); the free-text reason is never trusted.
- ``person_edit``: a person's address (``@users.corsia.local``).
- ``agent_revision``: an agent's address (``@agents.corsia.local``).
- ``outside_edit``: anything else, including an edit recorded from disk and ``CORSIA <corsia@localhost>``.

Accepts recorded before the trailer existed read by their author, as ``person_edit`` or
``agent_revision``. Formatting-only changes count as changes.

History holds only recorded versions. When ``as_of`` is at or after the document's latest version, the
working file is read once and bytes that differ from that version count as one more ``outside_edit``
version; for an earlier ``as_of``, history is all there is.

Version times are whole seconds, so a version saved in the same second as ``first_round_at`` or
``as_of`` may fall on either side of it. Every version that may have been current is weighed; when
they all give one answer that is the outcome, and ``changed_by`` names any same-second version too.

A run's outcome is None, never a partial answer, when:

- its artifact has no recorded document;
- no version is certainly at or before ``first_round_at`` (including the first version landing in
  that same second);
- the versions that may have been current give different answers (a version saved in the same second
  as ``first_round_at`` or ``as_of``), so it can't be told.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from infra.document_history import repository
from infra.guidance_git import _git_bytes
from infra.guidance_messages import accepted_suggestions

PERSON_EDIT = "person_edit"
ACCEPTED_SUGGESTION = "accepted_suggestion"
AGENT_REVISION = "agent_revision"
OUTSIDE_EDIT = "outside_edit"

Run = tuple[str, Any, Any]
"""(artifact_id, first_round_at, as_of); each time a ``datetime`` (naive is UTC), ISO text or epoch seconds."""


class WentAhead(TypedDict):
    went_ahead_unchanged: bool
    changed_by: list[str]


@dataclass(frozen=True)
class _Version:
    at: int
    writer: str
    blob: str | None  # None: this version deleted the file


_UNREAD = object()


def went_ahead_outcomes(
    runs: Sequence[Run], *, db=None, workspace: Path | None = None,
) -> dict[Run, WentAhead | None]:
    """Each run's outcome, keyed by the run as given; None for the three causes in the module
    docstring (no recorded document, no version certainly by ``first_round_at``, or same-second versions
    that disagree). A history Git can't read raises ``GitFault``."""
    runs = list(runs)
    if not runs:
        return {}
    from infra import config
    root = Path(workspace or config.WORKSPACE)
    paths = _artifact_paths(db, {run[0] for run in runs})
    repo = repository(root)
    relative = _relative_paths(repo, root, paths) if repo is not None else {}
    versions = _versions(repo, set(relative.values())) if relative else {}
    working: dict[str, str | None] = {}
    outcomes: dict[Run, WentAhead | None] = {}
    for run in runs:
        rel = relative.get(run[0])
        history = versions.get(rel, []) if rel is not None else []
        if rel is not None and history and rel not in working and _seconds(run[2]) >= history[-1].at:
            working[rel] = _working_blob(repo, rel, len(history[-1].blob or "") or _hash_length(versions))
        current = working.get(rel, _UNREAD) if rel is not None else _UNREAD
        outcomes[run] = _outcome(history, _seconds(run[1]), _seconds(run[2]), current)
    return outcomes


def went_ahead_outcome(
    artifact_id: str, first_round_at: Any, as_of: Any, *, db=None, workspace: Path | None = None,
) -> WentAhead | None:
    """One run's outcome; see :func:`went_ahead_outcomes`."""
    run = (artifact_id, first_round_at, as_of)
    return went_ahead_outcomes([run], db=db, workspace=workspace)[run]


def _possible(history: list[_Version], moment: float) -> list[int] | None:
    """Indexes of the versions that may have been current at ``moment``. Version times are whole seconds,
    so the last version before ``moment``'s second, and every version within it, all may have been;
    None when no version is certainly before it."""
    second = math.floor(moment)
    before = [index for index, version in enumerate(history) if version.at < second]
    if not before:
        return None
    return [before[-1], *(index for index, version in enumerate(history) if version.at == second)]


def _outcome(history: list[_Version], first: float, as_of: float, working) -> WentAhead | None:
    bases = _possible(history, first)
    if bases is None or any(history[index].blob is None for index in bases):
        return None
    blobs = [version.blob for version in history]
    writers = [version.writer for version in history]
    ends = _possible(history, as_of)
    if working is not _UNREAD and as_of >= history[-1].at and working != history[-1].blob:
        blobs.append(working)
        writers.append(OUTSIDE_EDIT)
        ends = [len(history)]
    if ends is None:
        return None
    same = {blobs[base] == blobs[end] for base in bases for end in ends if end >= base}
    if len(same) != 1:  # the versions the person may have seen disagree about this run
        return None
    if same == {True}:
        return {"went_ahead_unchanged": True, "changed_by": []}
    return {"went_ahead_unchanged": False, "changed_by": writers[min(bases) + 1:max(ends) + 1]}


def _artifact_paths(db, artifact_ids: set[str]) -> dict[str, str]:
    """The one database query: each artifact's stored path."""
    query = "SELECT id, path FROM artifact_identities WHERE id IN (SELECT value FROM json_each(?))"
    if db is None:
        from infra.database import get_db
        with closing(get_db()) as own:
            rows = own.execute(query, (json.dumps(sorted(artifact_ids)),)).fetchall()
    else:
        rows = db.execute(query, (json.dumps(sorted(artifact_ids)),)).fetchall()
    return {row[0]: row[1] for row in rows if row[1]}


def _relative_paths(repo: Path, root: Path, paths: dict[str, str]) -> dict[str, str]:
    relative = {}
    for artifact_id, path in paths.items():
        try:
            rel = (root / path).resolve(strict=False).relative_to(repo.resolve()).as_posix()
        except ValueError:  # outside this history: nothing recorded
            continue
        if "\n" not in rel:
            relative[artifact_id] = rel
    return relative


def _versions(repo: Path, paths: set[str]) -> dict[str, list[_Version]]:
    """Every recorded version of ``paths``, oldest first, from one ``git log`` of HEAD."""
    if not _git_bytes(repo, "rev-parse", "--verify", "--quiet", "HEAD", ok=(0, 1)).strip():
        return {}
    pathspecs = "".join(f":(literal){path}\n" for path in sorted(paths))
    raw = _git_bytes(
        repo, "log", "--raw", "--no-abbrev", "--no-renames", "-z",
        "--format=%x01%H%x00%ct%x00%ae%x00%B", "--stdin", "HEAD", data=f"--\n{pathspecs}".encode(),
    ).decode(errors="surrogateescape")
    found: dict[str, list[_Version]] = {}
    for at, email, message, changes in _commits(raw):
        writer = _writer(email, message)
        for path, blob in changes:
            if path in paths:
                found.setdefault(path, []).append(_Version(at, writer, blob))
    return {path: rows[::-1] for path, rows in found.items()}


def _commits(raw: str):
    """(commit time, author email, message, [(path, new blob or None)]) per commit, newest first."""
    chunks: list[str] = []
    for chunk in raw.split("\x01")[1:]:
        head = chunk.split("\0", 1)[0]
        if chunks and not (len(head) in (40, 64) and all(c in "0123456789abcdef" for c in head)):
            chunks[-1] += "\x01" + chunk  # a message that held \x01
        else:
            chunks.append(chunk)
    for chunk in chunks:
        _sha, at, email, rest = chunk.split("\0", 3)
        message, _, entries = rest.partition("\0\n")
        fields = entries.split("\0")
        changes = []
        for index in range(0, len(fields) - 1, 2):
            new = fields[index].split(" ")[3]
            changes.append((fields[index + 1], None if not new.strip("0") else new))
        yield int(at), email, message, changes


def _writer(email: str, message: str) -> str:
    if accepted_suggestions(message):
        return ACCEPTED_SUGGESTION
    if email.endswith("@users.corsia.local"):
        return PERSON_EDIT
    if email.endswith("@agents.corsia.local"):
        return AGENT_REVISION
    return OUTSIDE_EDIT


def _hash_length(versions: dict[str, list[_Version]]) -> int:
    return next((len(v.blob) for rows in versions.values() for v in rows if v.blob), 40)


def _working_blob(repo: Path, relative: str, length: int) -> str | None:
    """The Git blob id of the working file's bytes, or None when it is gone; read once per document."""
    try:
        data = (repo / relative).read_bytes()
    except FileNotFoundError:
        return None
    digest = hashlib.sha256 if length == 64 else hashlib.sha1
    return digest(b"blob %d\0" % len(data) + data).hexdigest()


def _seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()
