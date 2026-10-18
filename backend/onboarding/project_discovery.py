"""List the folders the user has been working in, under a one-time consent.

The read is the user's own Claude Code and Codex session history (#3288,
design r5 "Discovery read"). Governance conditions bound it:

- (a) The roots are the user's own ``~/.claude`` and ``~/.codex`` or an
  explicit override, never the backend process environment:
  ``CLAUDE_CONFIG_DIR`` and the managed Codex home hold CORSIA's own sessions.
- (b) At most ``HEAD_BYTES`` are read from each session file, and only the
  working-directory field is kept; no prompt, title or message text leaves.
- (c) ``is_git`` is a stat of ``<path>/.git``. No git command runs in a
  discovered folder, because a hostile config can execute code.

Before consent, ``session_apps`` only stats the fixed session folders under
the roots: it never lists, counts or opens anything in them.

Consent is option A of one answered find-projects decision (``setup_decisions``)
whose wording matches what CORSIA renders now, used once. ``consume_consent``
checks and records that use through the shared ``onboarding.consent``;
``discover`` reads.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from onboarding import consent

HEAD_BYTES = 64 * 1024
MAX_RESULTS = 50
CODEX_HORIZON_DAYS = 180

CLAUDE_CODE = "claude-code"
CODEX = "codex"

# --- consent contract -------------------------------------------------------

#: Operation and mutation-id prefix of the receipt that marks a consent used.
CONSENT_RECEIPT_OPERATION = "project_discovery_consent"

RAISE_FIRST = (
    "raise the discovery consent decision first: call project_discovery_list "
    "with no consent_decision_id for its exact fields, draft it with "
    "plan_decision, parent_review and raise it, then call again with its id "
    "once the user answers"
)
NO_SESSIONS = "no Claude Code or Codex session folders were found on this Mac"

CONSENT_TEXTS = consent.ConsentTexts(
    raise_first=RAISE_FIRST,
    wrong_wording=(
        "that decision does not carry the fixed discovery consent wording; " + RAISE_FIRST
    ),
    declined=(
        "the user did not choose to look for projects; do not list them "
        "and do not ask again unasked"
    ),
    used=(
        "that consent was already used for one listing; raise a new "
        "discovery consent decision to list again"
    ),
)


def consume_consent(db, decision_id: str, actor, wordings) -> None:
    """Accept one answered consent decision once, or raise ``ConsentRefused``.

    ``actor`` is the calling thread's ``PlanActor``. ``wordings`` are the
    (title, context, option A label) triples CORSIA renders now
    (``setup_decisions.consent_wordings``); the decision must carry one of them.
    The checks and the receipt are ``onboarding.consent``'s.
    """
    consent.consume(
        db, decision_id, actor, wordings, CONSENT_TEXTS,
        operation=CONSENT_RECEIPT_OPERATION, result={"consumed": True},
    )


# --- the read ---------------------------------------------------------------

CLAUDE_CODE_APP = "Claude Code"
CODEX_APP = "Codex"


def _home(folder: str):
    return lambda: Path(os.path.expanduser("~")) / folder


@dataclass(frozen=True)
class Roots:
    """The user's own tool roots. Defaults ignore the process environment.

    Discovery reads ``claude`` and ``codex``; ``agent_import`` also reads
    OpenClaw, Hermes (its default profile) and ``~/.agents``, Codex's shared
    skills folder.
    """

    claude: Path
    codex: Path
    openclaw: Path = field(default_factory=_home(".openclaw"))
    hermes: Path = field(default_factory=_home(".hermes"))
    agents: Path = field(default_factory=_home(".agents"))

    @classmethod
    def for_user(
        cls, claude: str | Path | None = None, codex: str | Path | None = None, *,
        openclaw: str | Path | None = None, hermes: str | Path | None = None,
        agents: str | Path | None = None,
    ) -> "Roots":
        home = Path(os.path.expanduser("~"))

        def root(named: str | Path | None, folder: str) -> Path:
            return Path(named).expanduser() if named else home / folder

        return cls(
            claude=root(claude, ".claude"), codex=root(codex, ".codex"),
            openclaw=root(openclaw, ".openclaw"), hermes=root(hermes, ".hermes"),
            agents=root(agents, ".agents"),
        )


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def session_apps(roots: Roots) -> list[str]:
    """The apps whose session folder exists, from one stat each; nothing is opened.

    A folder the process may not stat counts as absent.
    """
    return [
        app for app, folder in (
            (CLAUDE_CODE_APP, roots.claude / "projects"),
            (CODEX_APP, roots.codex / "sessions"),
        )
        if _is_dir(folder)
    ]


def _head(path: Path) -> bytes:
    """At most ``HEAD_BYTES`` from the start of one file; empty on failure."""
    try:
        with open(path, "rb") as handle:
            return handle.read(HEAD_BYTES)
    except OSError:
        return b""


def _claude_cwd(path: Path) -> str | None:
    """The first ``cwd`` in a Claude Code transcript head."""
    for line in _head(path).decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = record.get("cwd") if isinstance(record, dict) else None
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


_CWD_LITERAL = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')


def _codex_cwd(path: Path) -> str | None:
    """``payload.cwd`` of the ``session_meta`` first line of a Codex rollout.

    The first line can outgrow the byte cap (it may carry instructions), so a
    truncated line falls back to the ``cwd`` literal inside that same bounded
    prefix, which Codex writes ahead of those fields.
    """
    first = _head(path).split(b"\n", 1)[0].decode("utf-8", errors="replace")
    try:
        record = json.loads(first)
    except json.JSONDecodeError:
        if '"session_meta"' not in first:
            return None
        match = _CWD_LITERAL.search(first)
        cwd = json.loads(match.group(1)) if match else None
    else:
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            return None
        payload = record.get("payload")
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _claude_sessions(root: Path):
    """(cwd, mtime) from the newest transcript in each project directory."""
    projects = root / "projects"
    try:
        directories = [entry for entry in projects.iterdir() if entry.is_dir()]
    except OSError:
        return
    for directory in directories:
        try:
            files = [
                (mtime, path) for path in directory.glob("*.jsonl")
                if (mtime := _mtime(path)) is not None
            ]
        except OSError:
            continue
        if not files:
            continue
        mtime, newest = max(files)
        cwd = _claude_cwd(newest)
        if cwd:
            yield cwd, mtime


def _numeric_children(parent: Path) -> list[Path]:
    try:
        return [
            entry for entry in parent.iterdir()
            if entry.is_dir() and entry.name.isdigit()
        ]
    except OSError:
        return []


def _codex_sessions(root: Path, now: datetime.datetime):
    """(cwd, mtime) from each rollout in the last ``CODEX_HORIZON_DAYS``."""
    cutoff = (now - datetime.timedelta(days=CODEX_HORIZON_DAYS)).date()
    for year in _numeric_children(root / "sessions"):
        for month in _numeric_children(year):
            for day in _numeric_children(month):
                try:
                    dated = datetime.date(
                        int(year.name), int(month.name), int(day.name),
                    )
                except ValueError:
                    continue
                if dated < cutoff:
                    continue
                for path in day.glob("rollout-*.jsonl"):
                    mtime = _mtime(path)
                    cwd = _codex_cwd(path) if mtime is not None else None
                    if cwd:
                        yield cwd, mtime


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def discover(
    roots: Roots,
    *,
    excluded: Iterable[str | Path],
    now: datetime.datetime | None = None,
) -> list[dict]:
    """Folders sessions started in: newest first, deduped, at most 50.

    Each row is exactly ``{path, name, last_used, sources, is_git}``. A folder
    that no longer exists, or that is one of ``excluded`` (CORSIA's own
    workspace, state, install and config folders) or inside one, is left out.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    excluded_roots = tuple(os.path.realpath(str(root)) for root in excluded)
    seen: dict[str, dict] = {}
    for source, sessions in (
        (CLAUDE_CODE, _claude_sessions(roots.claude)),
        (CODEX, _codex_sessions(roots.codex, now)),
    ):
        for cwd, mtime in sessions:
            path = os.path.normpath(os.path.expanduser(cwd))
            if not os.path.isabs(path):
                continue
            key = os.path.realpath(path)
            entry = seen.get(key)
            if entry is None:
                entry = seen[key] = {"path": path, "mtime": mtime, "sources": set()}
            entry["mtime"] = max(entry["mtime"], mtime)
            entry["sources"].add(source)
    rows = []
    for key, entry in seen.items():
        if any(_inside(key, root) for root in excluded_roots) or not os.path.isdir(key):
            continue
        rows.append({
            "path": entry["path"],
            "name": os.path.basename(entry["path"]) or entry["path"],
            "last_used": datetime.datetime.fromtimestamp(
                entry["mtime"], datetime.timezone.utc,
            ).isoformat(timespec="seconds"),
            "sources": sorted(entry["sources"]),
            "is_git": os.path.exists(os.path.join(key, ".git")),
        })
    rows.sort(key=lambda row: (row["last_used"], row["path"]), reverse=True)
    return rows[:MAX_RESULTS]
