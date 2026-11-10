"""The workspace history's one ignore gate: a path the person's Git ignore rules keep out is never recorded.

A registered thread-folder file is the one exception the rules get, and a narrow one: a line that
ignores whole thread trays (``/.threads/*/*``, a project's ``projects/*/.threads/*/*``) does not keep
it out, since registering the file is the mark that it is kept. Every other rule still holds: the
workspace's other root lines, ``.git/info/exclude`` and the person's global excludes file. Git cannot
leave one line out of a check, so the tray check reads those sources itself and asks ``ls-files`` with
them; a ``.gitignore`` inside the workspace below its root is not read there, so a file under one is
kept out (never recorded rather than recorded against a rule).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_TRAY_LINE = re.compile(r"/?(projects/\*/)?\.threads(/\*){0,2}/?")


def ignored(repo: Path, rels: list[str], *, tray: bool = False) -> set[str]:
    """The ``rels`` history must not record; ``tray`` judges registered thread-folder files."""
    from infra.guidance_git import _git

    found = set(_git(repo, "check-ignore", "--", *rels, ok=(0, 1)).splitlines()) if rels else set()
    return _ignored_beside_trays(repo, sorted(found)) if tray and found else found


def _ignored_beside_trays(repo: Path, rels: list[str]) -> set[str]:
    """Of ``rels`` (each ignored by some rule), those still ignored once the tray-wide lines are left out."""
    from infra.guidance_git import _git, git_dir

    kept_out = {rel for rel in rels if not (repo / rel).is_file() or _nested_gitignore(repo, rel)}
    asked = [rel for rel in rels if rel not in kept_out]
    if not asked:
        return kept_out
    global_file = _git(repo, "config", "--path", "--get", "core.excludesFile", ok=(0, 1)).strip()
    default = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "ignore"
    # Later lines win, so the sources go lowest precedence first: global, then info/exclude, then the root.
    sources = [Path(global_file).expanduser() if global_file else default, git_dir(repo) / "info" / "exclude"]
    lines = [line for source in sources for line in _lines(source)]
    lines += [line for line in _lines(repo / ".gitignore") if not _TRAY_LINE.fullmatch(line.strip())]
    with tempfile.NamedTemporaryFile("w", prefix="corsia-history-ignore-", suffix=".txt") as patterns:
        patterns.write("\n".join(lines) + "\n")
        patterns.flush()
        listed = _git(repo, "ls-files", "--cached", "--others", "--ignored", f"--exclude-from={patterns.name}",
                      "--", *(f":(literal){rel}" for rel in asked))
    return kept_out | (set(listed.splitlines()) & set(asked))


def _nested_gitignore(repo: Path, rel: str) -> bool:
    folder = (repo / rel).parent
    while folder != repo and folder.is_relative_to(repo):
        if (folder / ".gitignore").exists():
            return True
        folder = folder.parent
    return False


def _lines(source: Path) -> list[str]:
    try:
        return source.read_text(errors="replace").splitlines()
    except OSError:
        return []
