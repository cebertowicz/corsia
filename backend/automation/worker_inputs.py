"""What a rule-started worker reads, and the brief it starts with (design §8.1).

A worker reads two things only: one directory in its rule owner's staging folder, and
the head tree the backend writes from a registered repository's object store (never
fetched) into the start's own directory, ``<owner thread root>/workers/<decision id>``.
That directory is a read path of the worker, never its working directory or an ancestor
of it, so a head's own ``.claude/``, ``CLAUDE.md`` or hooks are never loaded. It is
removed when the start settles. Anything else refuses the start with
:class:`StartRefused`, whose text the owner reads.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The request facts a ``start_worker`` rule's worker reads (named in the tool's description).
STAGING_FACT = "staging_path"
HEAD_REPO_FACT = "head_repo"
HEAD_SHA_FACT = "head_sha"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_GIT_TIMEOUT = 120


class StartRefused(Exception):
    """Why a start can't run; its text is what the owner is told."""


def owner_root(owner: str, project_id: str | None, db: sqlite3.Connection) -> Path:
    """The rule owner's thread root, the one its own confined children resolve."""
    from core.workspace_layout import resolve_thread_root
    from subagents.cc_runtime_helpers import _main_working_dir

    return resolve_thread_root(_main_working_dir(project_id, db), owner)


def start_dir(root: Path, decision_id: int) -> Path:
    return root / "workers" / str(decision_id)


def remove_start_dir(root: Path, decision_id: int) -> None:
    shutil.rmtree(start_dir(root, decision_id), ignore_errors=True)


def staging_dir(root: Path, value: Any) -> str | None:
    """The request's staging directory, only inside the owner's own staging folder."""
    if value is None or value == "":
        return None
    folder = (root / "staging").resolve()
    raw = Path(str(value))
    path = (raw if raw.is_absolute() else folder / raw).resolve()
    if path != folder and folder not in path.parents:
        raise StartRefused(f"refused: {STAGING_FACT} {value} is outside your staging folder {folder}")
    if not path.is_dir():
        raise StartRefused(f"refused: {STAGING_FACT} {value} is not a directory in your staging folder")
    return str(path)


def head_source(project_id: str | None, facts: Mapping[str, Any],
                db: sqlite3.Connection) -> tuple[Path, str, str] | None:
    """The request's (repository checkout, name, SHA), or ``None`` when it names no head
    repository. ``head_repo`` asks for a head: it must be registered with the project, and
    ``head_sha`` must name a commit. A ``head_sha`` alone (many events carry one) is only
    the item's version."""
    from infra.project_repo_policy import list_registered_repo_policies

    repo, sha = facts.get(HEAD_REPO_FACT), facts.get(HEAD_SHA_FACT)
    if repo in (None, ""):
        return None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise StartRefused(f"refused: {HEAD_REPO_FACT} needs {HEAD_SHA_FACT}, "
                           "a 40-character lowercase commit SHA")
    for policy in list_registered_repo_policies(project_id=project_id, db=db):
        if policy.repo_name == repo:
            return policy.path, repo, sha
    raise StartRefused(f"refused: {HEAD_REPO_FACT} {repo} is not a repository registered with your project")


def write_head(source: tuple[Path, str, str], dest: Path) -> None:
    """Write the head tree into ``dest/head`` from the repository's object store, never
    fetching: a commit not already there refuses the start."""
    cwd, repo, sha = source
    present = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd,
                             capture_output=True, timeout=_GIT_TIMEOUT)
    if present.returncode:
        raise StartRefused(f"refused: commit {sha[:12]} is not in {repo}'s object store; "
                           "fetch it there and request again")
    head = dest / "head"
    head.mkdir(parents=True)
    with subprocess.Popen(["git", "archive", "--format=tar", sha], cwd=cwd,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as archive:
        assert archive.stdout is not None
        try:
            with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
                tar.extractall(head, filter="data")
        except tarfile.TarError as exc:
            archive.kill()
            raise StartRefused(f"refused: commit {sha[:12]} could not be written: {exc}") from None
    if archive.returncode:
        raise StartRefused(f"refused: commit {sha[:12]} could not be read from {repo}")


def skill_body(path: Path) -> str:
    """A workflow skill's instructions, without its frontmatter."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def brief(workflow: str, body: str, schema: Mapping[str, Any], read_paths: list[str],
          event: Mapping[str, Any]) -> str:
    """The worker's one task: its workflow, what it may read, the event, and the answer's shape."""
    return "\n\n".join((
        f"You are a read-only worker running the workflow {workflow}. You can read only "
        f"these directories: {', '.join(read_paths)}. You cannot write, run commands or "
        "reach the network.",
        f"The event that started you: {event.get('type')}, with these facts:\n"
        f"{json.dumps(event.get('facts') or {}, ensure_ascii=False, indent=1, default=str)}",
        body,
        "End with one final message whose answer is a JSON value matching this schema, "
        "alone or in its first fenced json block:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=1)}",
    ))
