"""The person's own skills in their other AI tools: offers and installs.

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
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from onboarding.agent_import import (
    CLAUDE_CODE, CODEX, HERMES, LABELS, OPENCLAW, checked_root, display_path, tool_roots,
)
from onboarding.project_discovery import Roots

MAX_FILES = 100
MAX_BYTES = 512 * 1024
DESCRIPTION_CHARS = 500
HERMES_DEPTH = 3
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1"})
#: Frontmatter an installed copy keeps; every other key (``allowed-tools``, ``hooks``) is dropped.
KEPT_KEYS = ("license", "compatibility")

NEW = "new"
ALREADY_HERE = "already_here"
NAME_RESERVED = "name_reserved"
NAME_CLASH = "name_clash"
HAS_SECRET = "has_secret"
TOO_LARGE = "too_large"
UNREADABLE = "unreadable"

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

    @property
    def source(self) -> str:
        return f"{self.tool}:{display_path(self.path)}"

    @property
    def item_id(self) -> str:
        return f"{self.source}:{self.content_sha}"

    @property
    def skill_md(self) -> bytes:
        return self.files["SKILL.md"]


def _dirs(folder: Path) -> list[Path]:
    """Visible entries of a real folder that may be skills; links are kept to be refused."""
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return []
        return sorted(Path(entry.path) for entry in os.scandir(folder)
                      if not entry.name.startswith(".") and not entry.is_file(follow_symlinks=False))
    except OSError:
        return []


def _commands(folder: Path) -> list[tuple[Path, str]]:
    """Claude Code command files under ``commands/``, each with its flattened name."""
    found = []
    for directory, subfolders, names in os.walk(folder):
        subfolders[:] = sorted(name for name in subfolders if not name.startswith("."))
        for name in sorted(names):
            if name.endswith(".md") and not name.startswith("."):
                path = Path(directory) / name
                found.append((path, "-".join(path.relative_to(folder).with_suffix("").parts)))
    return found


def _hermes(folder: Path, depth: int = 0) -> list[Path]:
    """Hermes skill folders at any depth up to ``HERMES_DEPTH``, never inside a skill."""
    found = []
    for entry in _dirs(folder):
        if os.path.lexists(entry / "SKILL.md") or os.path.islink(entry):
            found.append(entry)
        elif depth + 1 < HERMES_DEPTH:
            found.extend(_hermes(entry, depth + 1))
    return found


def candidates(roots: Roots, tools: list[str]) -> list[Candidate]:
    """Every skill in the person's own folders of ``tools``, unread."""
    checked = {tool: real for tool, root in tool_roots(roots).items()
               if tool in tools and (real := checked_root(root)) is not None}
    found: list[Candidate] = []
    if claude := checked.get(CLAUDE_CODE):
        found += [Candidate(CLAUDE_CODE, path) for path in _dirs(claude / "skills")]
        found += [Candidate(CLAUDE_CODE, path, command) for path, command in _commands(claude / "commands")]
    if codex := checked.get(CODEX):
        found += [Candidate(CODEX, path) for path in _dirs(codex / "skills")]
        if (agents := checked_root(roots.agents)) is not None:
            found += [Candidate(CODEX, path) for path in _dirs(agents / "skills")]
    if openclaw := checked.get(OPENCLAW):
        for folder in (openclaw / "workspace" / "skills", openclaw / "skills"):
            found += [Candidate(OPENCLAW, path) for path in _dirs(folder)]
    if hermes := checked.get(HERMES):
        found += [Candidate(HERMES, path) for path in _hermes(hermes / "skills")]
    return found


def _listing(candidate: Candidate) -> list[tuple[str, Path]] | str:
    """(relative name, path) of every visible file, or the state that stops the offer."""
    if candidate.command is not None:
        if not stat.S_ISREG(os.lstat(candidate.path).st_mode):
            return UNREADABLE
        return [("SKILL.md", candidate.path)]
    if not stat.S_ISDIR(os.lstat(candidate.path).st_mode):
        return UNREADABLE
    listed: list[tuple[str, Path]] = []
    size = 0
    for directory, subfolders, names in os.walk(candidate.path):
        subfolders[:] = [name for name in subfolders if not name.startswith(".")]
        for name in subfolders:
            if os.path.islink(Path(directory) / name):
                return UNREADABLE
        for name in names:
            if name.startswith("."):
                continue
            path = Path(directory) / name
            mode = os.lstat(path)
            if not stat.S_ISREG(mode.st_mode):
                return UNREADABLE
            size += mode.st_size
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
            return UNREADABLE
        if "\x00" in texts[name]:
            return UNREADABLE
        candidate.files[name] = data
    if "SKILL.md" not in texts:
        return UNREADABLE
    digest = hashlib.sha256()
    for name in sorted(candidate.files):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(candidate.files[name]).digest())
    candidate.content_sha = digest.hexdigest()
    try:
        meta, body = parse_frontmatter(texts["SKILL.md"])
    except (ValueError, yaml.YAMLError):
        return UNREADABLE
    fallback = candidate.command or candidate.path.name
    try:
        candidate.name = validate_skill_dir_name(text_value(meta.get("name")) or fallback)
    except ValueError:
        candidate.name = fallback
        return UNREADABLE
    first_line = next((line.lstrip("# ").strip() for line in body.splitlines() if line.strip()), "")
    described = text_value(meta.get("description")) or (first_line if candidate.command else "")
    candidate.description = shown_description(described)
    if not candidate.description:
        return UNREADABLE
    if any(contains_credential(text) for text in texts.values()):
        return HAS_SECRET
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
            candidate.state = UNREADABLE
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
        if row.state in (TOO_LARGE, UNREADABLE):
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
    so a held-back skill's text (a key in its description included) never reaches it."""
    scripts = sum(1 for name in row.files if name != "SKILL.md" and Path(name).suffix in SCRIPT_SUFFIXES)
    return {"item_id": row.item_id, "name": row.name, "description": row.description if row.state == NEW else "",
            "tools": row.tools, "scripts": scripts, "state": row.state}


def skill_offers(roots: Roots, tools: list[str], workspace: Path) -> list[dict]:
    return [offer(row) for row in scan(roots, tools, workspace)]


def installed_text(row: Candidate, now: datetime.datetime) -> tuple[str, str]:
    """The ``SKILL.md`` and ``corsia.yaml`` an install writes for ``row``."""
    import yaml

    from core.skill_sidecar import FORMAT_KEY, POINTER_KEY, SIDECAR_FILE, SIDECAR_FORMAT
    from core.skill_validation import parse_frontmatter

    meta, body = parse_frontmatter(row.skill_md.decode("utf-8"))
    source_metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    metadata = {key: value for key, value in source_metadata.items() if key not in (POINTER_KEY, FORMAT_KEY)}
    metadata.update({POINTER_KEY: SIDECAR_FILE, FORMAT_KEY: SIDECAR_FORMAT})
    front = {"name": row.name, "description": row.description,
             **{key: meta[key] for key in KEPT_KEYS if key in meta}, "metadata": metadata}
    skill_md = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n" + body
    origin = {"source": row.source, "sha256": hashlib.sha256(row.skill_md).hexdigest(),
              "adopted_at": now.isoformat(timespec="seconds")}
    return skill_md, yaml.safe_dump({"origin": origin}, sort_keys=False)


def _write(target: Path, name: str, data: bytes) -> None:
    path = target / name
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(path, 0o644)


def _install(row: Candidate, skills: Path, now: datetime.datetime) -> str:
    from core.skill_sidecar import SIDECAR_FILE
    from core.skill_validation import validate_skill_bundle, validate_skill_file

    skill_md, sidecar = installed_text(row, now)
    if not validate_skill_bundle(skill_md, {SIDECAR_FILE: sidecar}).ok:
        return "failed"
    skills.mkdir(parents=True, exist_ok=True)
    target = skills / row.name
    if Path(os.path.realpath(target.parent)) != Path(os.path.realpath(skills)):
        return "failed"
    try:
        target.mkdir()
    except FileExistsError:
        return ALREADY_HERE
    try:
        _write(target, "SKILL.md", skill_md.encode("utf-8"))
        _write(target, SIDECAR_FILE, sidecar.encode("utf-8"))
        for name, data in sorted(row.files.items()):
            if name not in ("SKILL.md", SIDECAR_FILE):
                _write(target, name, data)
        if validate_skill_file(target / "SKILL.md").ok:
            return "installed"
    except OSError:
        pass
    shutil.rmtree(target, ignore_errors=True)
    return "failed"


def install_skills(roots: Roots, tools: list[str], workspace: Path, item_ids: list[str], *,
                   now: datetime.datetime | None = None) -> list[dict]:
    """Install each picked item that is still ``new`` with the content that was shown."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rows = {row.item_id: row for row in scan(roots, tools, workspace)}
    skills = _installed(workspace)[2]
    results = []
    for item_id in dict.fromkeys(item_ids):
        row = rows.get(item_id)
        if row is None or row.state != NEW:
            outcome = "changed" if row is None else row.state
            results.append({"item_id": item_id, "name": row.name if row else "", "outcome": outcome})
            continue
        results.append({"item_id": item_id, "name": row.name, "sha256": row.content_sha,
                        "outcome": _install(row, skills, now)})
    return results
