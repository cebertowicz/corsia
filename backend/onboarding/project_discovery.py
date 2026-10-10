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
checks and records that use; ``discover`` reads.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

HEAD_BYTES = 64 * 1024
MAX_RESULTS = 50
CODEX_HORIZON_DAYS = 180

CLAUDE_CODE = "claude-code"
CODEX = "codex"

# --- consent contract -------------------------------------------------------

#: The option that authorizes reading session folders, in the consent decision.
CONSENT_OPTION_ID = "A"

#: Operation and mutation-id prefix of the receipt that marks a consent used.
CONSENT_RECEIPT_OPERATION = "project_discovery_consent"

RAISE_FIRST = (
    "raise the discovery consent decision first: call project_discovery_list "
    "with no consent_decision_id for its exact fields, draft it with "
    "plan_decision, parent_review and raise it, then call again with its id "
    "once the user answers"
)
NO_SESSIONS = "no Claude Code or Codex session folders were found on this Mac"


def consent_wording_hash(title: str, context: str, option_label: str) -> str:
    """Hash the three strings the user read when consenting."""
    payload = json.dumps([title, context, option_label], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConsentRefused(Exception):
    """The consent decision cannot authorize a listing; the message says why."""


# Plan-table reads remain owned by the narrow public Plans interface below.

def consume_consent(db, decision_id: str, actor, wordings) -> None:
    """Accept one answered consent decision once, or raise ``ConsentRefused``.

    ``actor`` is the calling thread's ``PlanActor``. ``wordings`` are the
    (title, context, option A label) triples CORSIA renders now
    (``setup_decisions.consent_wordings``); the decision must carry one of them.
    Use is recorded in the caller's transaction before anything is read, so a
    crash mid-listing still spends the consent rather than leaving it replayable.
    """
    from plans import public

    try:
        plan, decision, _source, bound = public.resolve_decision(
            db, decision_id, actor, settled_source_ok=True,
        )
    except (PermissionError, public.PlanError) as exc:
        raise ConsentRefused(
            f"that decision is not in your plan; {RAISE_FIRST}"
        ) from exc
    if plan["owner_id"] != actor.thread_id:
        raise ConsentRefused(f"that decision is not in your plan; {RAISE_FIRST}")
    state = decision["state"]
    if state in ("draft", "parent_reviewed"):
        raise ConsentRefused(
            "that decision has not been raised to the user; raise it and wait "
            "for the user's answer"
        )
    if state == "raised":
        raise ConsentRefused(
            "the user has not answered that decision yet; wait for the answer"
        )
    if state != "answered":
        raise ConsentRefused(f"that decision was withdrawn; {RAISE_FIRST}")
    consent_option = next(
        (o for o in decision["options"] if o["id"] == CONSENT_OPTION_ID), None,
    )
    wording = consent_wording_hash(
        decision["title"], decision["context"],
        consent_option["label"] if consent_option else "",
    )
    if wording not in {consent_wording_hash(*expected) for expected in wordings}:
        raise ConsentRefused(
            "that decision does not carry the fixed discovery consent wording; "
            + RAISE_FIRST
        )
    answered_by = public.decision_answer_actor(db, node_id=decision["node_id"])
    if not (answered_by or "").startswith("user:"):
        raise ConsentRefused("that decision was not answered by the user")
    chosen = public.chosen_ids(decision, decision.get("answer"))
    if public.pick_of(decision) == public.PICK_ANY or chosen != [CONSENT_OPTION_ID]:
        raise ConsentRefused(
            "the user did not choose to look for projects; do not list them "
            "and do not ask again unasked"
        )
    mutation_id = f"{CONSENT_RECEIPT_OPERATION}:{decision['id']}"
    fingerprint = public.request_fingerprint(
        CONSENT_RECEIPT_OPERATION, {"decision_id": decision["id"]},
    )
    try:
        used = public.load_mutation_receipt(
            db, plan_id=plan["id"], mutation_id=mutation_id,
            actor_id=bound.audit_id, operation=CONSENT_RECEIPT_OPERATION,
            fingerprint=fingerprint,
        )
    except public.PlanError as exc:
        raise ConsentRefused(
            "that consent was already used for one listing; raise a new "
            "discovery consent decision to list again"
        ) from exc
    if used is not None:
        raise ConsentRefused(
            "that consent was already used for one listing; raise a new "
            "discovery consent decision to list again"
        )
    public.record_mutation_receipt(
        db, plan_id=plan["id"], mutation_id=mutation_id,
        actor_id=bound.audit_id, operation=CONSENT_RECEIPT_OPERATION,
        fingerprint=fingerprint, result={"consumed": True},
    )


# --- the read ---------------------------------------------------------------

CLAUDE_CODE_APP = "Claude Code"
CODEX_APP = "Codex"


@dataclass(frozen=True)
class Roots:
    """The user's own history roots. Defaults ignore the process environment."""

    claude: Path
    codex: Path

    @classmethod
    def for_user(
        cls, claude: str | Path | None = None, codex: str | Path | None = None,
    ) -> "Roots":
        home = Path(os.path.expanduser("~"))
        return cls(
            claude=Path(claude).expanduser() if claude else home / ".claude",
            codex=Path(codex).expanduser() if codex else home / ".codex",
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
