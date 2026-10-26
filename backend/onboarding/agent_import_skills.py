"""The person's own skills in their other tools: offers and installs.

Only the person's own skill folders are scanned (plugin, bundled and ``.system``
skills never are). Each skill is bounded by lstat before anything is hashed, and
any link, binary file or unreadable part keeps it from being offered. The agent
receives name, description, tools, script count and offer state, never a body.
An install re-reads the folder, writes only an offer still ``new`` with the same
content hash, and writes CORSIA's skill data form: the whitelisted frontmatter,
a ``corsia.yaml`` holding only a home-relative ``origin``, and no overwrite.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from onboarding.agent_import import (
    CLAUDE_CODE, CODEX, HERMES, LABELS, OPENCLAW, TOOL_MADE_SKILL_FOLDERS, inside, checked_root, display_path,
    tool_roots,
)
from onboarding.project_discovery import Roots

MAX_FILES = 100
MAX_BYTES = 512 * 1024
DESCRIPTION_CHARS = 500
HERMES_DEPTH = 3
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1"})

NEW = "new"
ALREADY_HERE = "already_here"
NAME_RESERVED = "name_reserved"
NAME_CLASH = "name_clash"
HAS_SECRET = "has_secret"
TOO_LARGE = "too_large"
UNREADABLE = "unreadable"
#: Why a skill is ``unreadable``, so the person hears the one thing to fix; "" when unknown.
LINK, NOT_TEXT, NO_SKILL_MD, BAD_HEADER, BAD_NAME, NO_DESCRIPTION = (
    "link", "not_text", "no_skill_md", "bad_header", "bad_name", "no_description")

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class Candidate:
    """One skill found in a tool's folder."""

    tool: str
    path: Path
    command: str | None = None
    state: str = NEW
    name: str = ""
    description: str = ""
    files: dict[str, bytes] = field(default_factory=dict)
    content_sha: str = ""
    tools: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def source(self) -> str:
        return f"{self.tool}:{display_path(self.path)}"

    @property
    def item_id(self) -> str:
        """Only a ``new`` row's id binds its content hash: a held-back row's (a key in it
        included) never carries a digest of its bytes to the agent."""
        return f"{self.source}:{self.content_sha}" if self.state == NEW else self.source

    @property
    def skill_md(self) -> bytes:
        return self.files["SKILL.md"]


def _real_folder(folder: Path, root: Path) -> bool:
    """``folder`` is itself no link and resolves inside ``root``, so no linked parent leads out."""
    try:
        return stat.S_ISDIR(os.lstat(folder).st_mode) and inside(Path(os.path.realpath(folder)), root)
    except OSError:
        return False


def _dirs(folder: Path, root: Path) -> list[Path]:
    """Visible entries of a real folder that may be skills; links are kept to be refused."""
    try:
        if not _real_folder(folder, root):
            return []
        return sorted(Path(entry.path) for entry in os.scandir(folder)
                      if not entry.name.startswith(".") and not entry.is_file(follow_symlinks=False))
    except OSError:
        return []


def _commands(folder: Path, root: Path) -> list[tuple[Path, str]]:
    """Claude Code command files under ``commands/``, each with its flattened name."""
    found: list[tuple[Path, str]] = []
    if not _real_folder(folder, root):
        return found
    for directory, subfolders, names in os.walk(folder):
        subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
        for name in sorted(names):
            if name.endswith(".md") and not name.startswith("."):
                path = Path(directory) / name
                found.append((path, "-".join(path.relative_to(folder).with_suffix("").parts)))
    return found


def _hermes(folder: Path, root: Path, depth: int = 0) -> list[Path]:
    """Hermes skill folders at any depth up to ``HERMES_DEPTH``, never inside a skill."""
    found = []
    for entry in _dirs(folder, root):
        if os.path.lexists(entry / "SKILL.md") or os.path.islink(entry):
            found.append(entry)
        elif depth + 1 < HERMES_DEPTH:
            found.extend(_hermes(entry, root, depth + 1))
    return found


def candidates(roots: Roots, tools: list[str]) -> list[Candidate]:
    """Every skill in the person's own folders of ``tools``, unread."""
    checked = {tool: real for tool, root in tool_roots(roots).items()
               if tool in tools and (real := checked_root(root)) is not None}
    found: list[Candidate] = []
    if claude := checked.get(CLAUDE_CODE):
        found += [Candidate(CLAUDE_CODE, path) for path in _dirs(claude / "skills", claude)
                  if path.name not in TOOL_MADE_SKILL_FOLDERS[CLAUDE_CODE]]
        found += [Candidate(CLAUDE_CODE, path, command)
                  for path, command in _commands(claude / "commands", claude)]
    if codex := checked.get(CODEX):
        found += [Candidate(CODEX, path) for path in _dirs(codex / "skills", codex)]
        if (agents := checked_root(roots.agents)) is not None:
            found += [Candidate(CODEX, path) for path in _dirs(agents / "skills", agents)]
    if openclaw := checked.get(OPENCLAW):
        for folder in (openclaw / "workspace" / "skills", openclaw / "skills"):
            found += [Candidate(OPENCLAW, path) for path in _dirs(folder, openclaw)]
    if hermes := checked.get(HERMES):
        found += [Candidate(HERMES, path) for path in _hermes(hermes / "skills", hermes)]
    return found


def _unreadable(candidate: Candidate, reason: str) -> str:
    candidate.reason = reason
    return UNREADABLE


def _listing(candidate: Candidate) -> list[tuple[str, Path]] | str:
    """(relative name, path) of every visible file, or the state that stops the offer."""
    mode = os.lstat(candidate.path).st_mode
    if candidate.command is not None:
        if not stat.S_ISREG(mode):
            return _unreadable(candidate, LINK if stat.S_ISLNK(mode) else NOT_TEXT)
        return [("SKILL.md", candidate.path)]
    if not stat.S_ISDIR(mode):
        return _unreadable(candidate, LINK if stat.S_ISLNK(mode) else NOT_TEXT)
    listed: list[tuple[str, Path]] = []
    size = 0
    for directory, subfolders, names in os.walk(candidate.path):
        subfolders[:] = [name for name in subfolders if not name.startswith(".")]
        for name in subfolders:
            if os.path.islink(Path(directory) / name):
                return _unreadable(candidate, LINK)
        for name in names:
            if name.startswith("."):
                continue
            path = Path(directory) / name
            entry = os.lstat(path)
            if not stat.S_ISREG(entry.st_mode):
                return _unreadable(candidate, LINK if stat.S_ISLNK(entry.st_mode) else NOT_TEXT)
            size += entry.st_size
            listed.append((path.relative_to(candidate.path).as_posix(), path))
            if len(listed) > MAX_FILES or size > MAX_BYTES:
                return TOO_LARGE
    return listed


def shown_description(text: str) -> str:
    """Control characters gone, line breaks collapsed, at most 500 characters cut at a word."""
    flat = " ".join(_CONTROL.sub(" ", text).split())
    if len(flat) <= DESCRIPTION_CHARS:
        return flat
    cut = flat[:DESCRIPTION_CHARS - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip() + "…"


def _parse(candidate: Candidate) -> str:
    """Read the files, name and description; the state the content alone decides."""
    import yaml

    from core.skill_validation import parse_frontmatter, validate_skill_dir_name
    from core.skill_workflow_validation import text_value
    from host_access.credential_sanitizer import contains_credential

    listed = _listing(candidate)
    if isinstance(listed, str):
        return listed
    texts = {}
    for name, path in listed:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            return _unreadable(candidate, NOT_TEXT)
        if "\x00" in texts[name]:
            return _unreadable(candidate, NOT_TEXT)
        candidate.files[name] = data
    from core.skill_sidecar import installed_digest_part

    digest = hashlib.sha256()
    for name in sorted(candidate.files):
        try:
            part = installed_digest_part(name, candidate.files[name])
        except (UnicodeDecodeError, ValueError):
            # A malformed sidecar is still part of the offered bundle identity.
            part = candidate.files[name]
        if part is not None:
            digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(part).digest())
    candidate.content_sha = digest.hexdigest()
    # A key is reported before any layout problem, so fixing the layout never comes first.
    if any(contains_credential(text) for text in texts.values()):
        return HAS_SECRET
    if "SKILL.md" not in texts:
        return _unreadable(candidate, NO_SKILL_MD)
    try:
        meta, body = parse_frontmatter(texts["SKILL.md"])
    except (ValueError, yaml.YAMLError):
        return _unreadable(candidate, BAD_HEADER)
    fallback = candidate.command or candidate.path.name
    try:
        candidate.name = validate_skill_dir_name(text_value(meta.get("name")) or fallback)
    except ValueError:
        candidate.name = fallback
        return _unreadable(candidate, BAD_NAME)
    first_line = next((line.lstrip("# ").strip() for line in body.splitlines() if line.strip()), "")
    described = text_value(meta.get("description")) or (first_line if candidate.command else "")
    candidate.description = shown_description(described)
    if not candidate.description:
        return _unreadable(candidate, NO_DESCRIPTION)
    return NEW


def _installed(workspace: Path) -> tuple[set[str], set[str], Path]:
    """Names in the workspace skills, the upstream hashes imported there, and its skills folder."""
    from core.skill_resolution import local_skill_inventory, skill_scope_root
    from core.skill_sidecar import parse_sidecar, read_sidecar

    rows = local_skill_inventory(scope="workspace", workspace=workspace, db=None)
    hashes = set()
    for row in rows:
        text, _problem = read_sidecar(Path(row.path).parent)
        data = parse_sidecar(text)[0] if text else None
        origin = data.get("origin") if isinstance(data, dict) else None
        if isinstance(origin, dict) and isinstance(origin.get("sha256"), str):
            hashes.add(origin["sha256"])
    return {row.name for row in rows}, hashes, skill_scope_root(scope="workspace", workspace=workspace, db=None) / "skills"


def scan(roots: Roots, tools: list[str], workspace: Path) -> list[Candidate]:
    """One row per distinct skill, each with its offer state."""
    from core.system_tier import system_skill_names

    names, hashes, skills = _installed(workspace)
    reserved = system_skill_names()
    rows: list[Candidate] = []
    by_name: dict[str, list[Candidate]] = {}
    for candidate in candidates(roots, tools):
        try:
            candidate.state = _parse(candidate)
        except OSError:
            candidate.state, candidate.reason = UNREADABLE, ""
        candidate.name = candidate.name or candidate.command or candidate.path.name
        same = next((row for row in by_name.get(candidate.name, ())
                     if row.content_sha and row.content_sha == candidate.content_sha), None)
        if same is not None:
            if LABELS[candidate.tool] not in same.tools:
                same.tools.append(LABELS[candidate.tool])
            continue
        candidate.tools = [LABELS[candidate.tool]]
        by_name.setdefault(candidate.name, []).append(candidate)
        rows.append(candidate)
    for row in rows:
        if not reserved:
            # System skill names unknown: any of these could shadow one, so none is offered.
            row.state, row.reason = UNREADABLE, ""
            continue
        # A key outranks a name state: its warning stays, and install never compares its hash.
        if row.state in (HAS_SECRET, TOO_LARGE, UNREADABLE):
            continue
        if row.name in reserved:
            row.state = NAME_RESERVED
        elif (row.name in names or os.path.lexists(skills / row.name)
              or hashlib.sha256(row.skill_md).hexdigest() in hashes):
            row.state = ALREADY_HERE
        elif len(by_name[row.name]) > 1:
            row.state = NAME_CLASH
    return rows


def offer(row: Candidate) -> dict:
    """What the agent sees of one skill: never its body, and a description only for a new one,
    so a held-back skill's text (a key in its description included) never reaches it. An
    ``unreadable`` row adds its ``reason``, a fixed word, so the person hears what to fix."""
    scripts = sum(1 for name in row.files if name != "SKILL.md" and Path(name).suffix in SCRIPT_SUFFIXES)
    shown = {"item_id": row.item_id, "name": row.name, "description": row.description if row.state == NEW else "",
             "tools": row.tools, "scripts": scripts, "state": row.state}
    if row.state == UNREADABLE:
        shown["reason"] = row.reason
    return shown


def skill_offers(roots: Roots, tools: list[str], workspace: Path) -> list[dict]:
    return [offer(row) for row in scan(roots, tools, workspace)]


def _install(row: Candidate, workspace: Path, now: datetime.datetime, *, author_thread_id: str,
             consent_decision_id: str | None) -> dict[str, str]:
    """Install one offered skill through the shared guidance-history writer."""
    from infra.skill_install import install_skill_bundle, origin_metadata

    return install_skill_bundle(
        workspace,
        row.name,
        row.skill_md,
        {name: data for name, data in row.files.items() if name != "SKILL.md"},
        row.description,
        origin_metadata(row.source, row.skill_md, now.isoformat(timespec="seconds")),
        author_thread_id=author_thread_id,
        consent_decision_id=consent_decision_id,
    )


def install_skills(roots: Roots, tools: list[str], workspace: Path, item_ids: list[str], *,
                   now: datetime.datetime | None = None, author_thread_id: str = "",
                   consent_decision_id: str | None = None) -> list[dict]:
    """Install each picked item that is still ``new`` with the content that was shown."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    scanned = scan(roots, tools, workspace)
    rows = {row.item_id: row for row in scanned}
    # Only rows whose bytes passed the key and parse checks are compared with an offered hash;
    # comparing any other row's would confirm a guess at bytes the agent was never shown.
    held = {row.source: row for row in scanned if row.state in (ALREADY_HERE, NAME_RESERVED, NAME_CLASH)}
    results = []
    for item_id in dict.fromkeys(item_ids):
        # An offered id is ``<source>:<hash>``; a row held back since with that content is
        # known by its source, so its state is reported. Anything else is ``changed``.
        source, _, shown = item_id.rpartition(":")
        row = rows.get(item_id)
        if row is None and (same := held.get(source)) is not None and same.content_sha == shown:
            row = same
        if row is None or row.state != NEW:
            outcome = "changed" if row is None else row.state
            results.append({"item_id": item_id, "name": row.name if row else "", "outcome": outcome})
            continue
        results.append({"item_id": item_id, "name": row.name, "sha256": row.content_sha,
                        **_install(row, workspace, now, author_thread_id=author_thread_id,
                                   consent_decision_id=consent_decision_id)})
    return results
