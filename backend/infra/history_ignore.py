"""The workspace history's one ignore gate: a path the person's Git ignore rules keep out is never recorded.

Git answers which rule decides each path (``check-ignore -v``). A registered thread-folder file is the
one exception, and a narrow one: when that rule is a root ``.gitignore`` line ignoring whole thread trays
(``/.threads/*/*``, a project's ``projects/*/.threads/*/*``), it is recorded anyway, since registering the
file is the mark that it is kept. Any other deciding rule keeps it out. Git reports the first excluded
folder on a path's way down, so under such a line it names the tray line for a file in a tray's
sub-folder even where another rule would also ignore it.

A credential-shaped file name is never recorded, whatever the ignore rules say: history keeps every
commit, so a key saved once could never be taken back out, and a workspace made before the scaffold
carried these lines has no rule for them.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

_TRAY_LINE = re.compile(r"/?(projects/\*/)?\.threads(/\*){0,2}/?")
# The workspace scaffold writes these same names into a new workspace's .gitignore.
CREDENTIAL_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "id_rsa*", "id_ed25519*")


def _credential(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return any(fnmatchcase(name, pattern) for pattern in CREDENTIAL_NAMES)


def ignored(repo: Path, rels: list[str], *, tray: bool = False) -> set[str]:
    """The ``rels`` history must not record; ``tray`` judges registered thread-folder files."""
    from infra.guidance_git import _git_bytes

    found = {rel for rel in rels if _credential(rel)}
    rels = [rel for rel in rels if rel not in found]
    if not rels:
        return found
    paths = b"".join(rel.encode(errors="surrogateescape") + b"\0" for rel in rels)
    out = _git_bytes(repo, "check-ignore", "-v", "-z", "--stdin", data=paths, ok=(0, 1))
    fields = out.decode(errors="surrogateescape").split("\0")
    for source, _line, pattern, path in zip(*[iter(fields[:-1])] * 4):
        if pattern.startswith("!"):
            continue  # a negated rule decided it: not ignored
        if tray and source == ".gitignore" and _TRAY_LINE.fullmatch(pattern):
            continue
        found.add(path)
    return found
