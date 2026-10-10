"""Detect shell writes that bypass durable shared-document governance."""

from __future__ import annotations

import logging
import os
import shlex
from fnmatch import fnmatchcase
from pathlib import Path

from infra.database import get_db, write_transaction
from registry import service
from registry.durable_writes import _bounded_shared_files
from registry.identity_metadata import last_written_sha256, sha256_file
from registry.shared_document_events import report_owner_write
from registry.shared_documents import _scope_root
from registry.shared_write_errors import ThreadUnavailableError
from registry.shared_principals import (
    WriteAuthority,
    artifact_owner,
    document_scope,
    may_edit_document,
    scope_relative,
)


log = logging.getLogger(__name__)


async def collect_shell_shared_write_notices(
    thread_id: str,
    project_id: str | None,
    command: str,
    started_at_ns: int,
    cwd: str | Path,
    *,
    runner,
) -> list[str]:
    """Run the blocking detector and make an inspection failure visible."""
    try:
        return await runner(
            detect_shell_shared_writes,
            thread_id,
            project_id,
            command,
            started_at_ns,
            cwd,
        ) or []
    except ThreadUnavailableError:
        return []
    except Exception:
        log.exception("shared write detection failed in %s", thread_id)
        return ["Warning: CORSIA could not inspect this command's shared-file writes."]


def _changed_since(path: Path, started_at_ns: int) -> bool:
    try:
        return path.stat().st_mtime_ns >= started_at_ns
    except OSError:
        return False


_SHELL_OPERATORS = frozenset("();<>|&")


def _command_paths(command: str, cwd: Path) -> tuple[set[str], list[str]]:
    """Return the exact paths and glob patterns a command names.

    Relative words resolve against the working directory and every ``cd``
    target the command names.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        words = command.split()
    bases = [cwd]
    for previous, word in zip(["", *words], words):
        if previous == "cd":
            bases.append(cwd / os.path.expanduser(word))
    exact: set[str] = set()
    patterns: list[str] = []
    for word in words:
        if not word or set(word) <= _SHELL_OPERATORS:
            continue
        word = os.path.expanduser(word.rpartition("=")[2] if word.startswith("-") else word)
        for base in bases:
            candidate = os.path.normpath(base / word)
            if any(char in word for char in "*?["):
                patterns.append(candidate)
            else:
                exact.update({candidate, os.path.realpath(candidate)})
    return exact, patterns


def _named_by_command(path: Path, exact: set[str], patterns: list[str]) -> bool:
    """Whether the command text is evidence that it wrote ``path``."""
    names = {os.path.normpath(path), os.path.realpath(path)}
    return bool(names & exact) or any(
        fnmatchcase(name, pattern) for name in names for pattern in patterns
    )


def detect_shell_shared_writes(
    thread_id: str,
    project_id: str | None,
    command: str,
    started_at_ns: int,
    cwd: str | Path,
) -> list[str]:
    """Report owned shared files one foreground shell command changed to their owners.

    The command is decided like any other write to each file it changed; a
    refused change landed anyway, so the writer is told to suggest instead.
    A changed file is attributed to the command only when the command names
    it; an mtime inside the command's window alone cannot tell this command
    from a concurrent writer. The walk, stats and hashes run before the write
    transaction, which is taken only when an owner needs telling.
    """
    from infra.config import WORKSPACE

    db = get_db()
    try:
        scope_root, authoritative_project = _scope_root(
            workspace=WORKSPACE,
            thread_id=thread_id,
            project_id=project_id,
            direct_user=False,
            db=db,
        )
        changed = [
            path
            for path in _bounded_shared_files(scope_root / "shared")
            if _changed_since(path, started_at_ns)
        ]
        if changed:
            exact, patterns = _command_paths(command, Path(cwd))
            changed = [path for path in changed if _named_by_command(path, exact, patterns)]
        if not changed:
            return []
        _, conductor = document_scope(changed[0], workspace=WORKSPACE, db=db)
        reports: list[tuple[Path, dict, WriteAuthority]] = []
        for path in changed:
            artifact = service.find_file_identity(str(path), db=db)
            if artifact is None:
                continue
            owner = artifact_owner(str(artifact["id"]), db=db)
            if not owner:
                continue
            stored = last_written_sha256(artifact.get("metadata"))
            if stored and sha256_file(path) == stored:
                continue
            authority = may_edit_document(
                thread_id, False, owner=str(owner), conductor=conductor,
                path_rel=scope_relative(path, scope_root), artifact_id=str(artifact["id"]), db=db,
            )
            if authority.reaches_owner:
                reports.append((path, artifact, authority))
        if not reports:
            return []
        with write_transaction(db, scope="detect_shell_shared_writes") as connection:
            notices = [
                report_owner_write(authority, artifact, path=path, before=None, after=None,
                                   thread_id=thread_id, db=connection, source="shell")
                for path, artifact, authority in reports
            ]
        return [notice for notice in notices if notice]
    finally:
        db.close()
