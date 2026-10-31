"""A project- or workspace-wide lesson becomes one backlog issue for the conductor.

A worker cannot write another thread's plan (plans are actor-bound), and the
project's intake route for candidate work is a GitHub issue under the owning
lane's label (coordination model, Work intake). The issue carries the lesson
rewritten as an impersonal rule, never the entry text or the user's words, and
a marker that makes creation idempotent across restarts.

The issue goes to the repository of the thread's own project, as its project
repository policy registers it; workspace-wide lessons too. Nothing is filed
anywhere else: with no registered GitHub repository the lesson stays unplaced.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_LANE = re.compile(r"Backlog lane:\s*(lane:[a-z0-9-]+)")
_REMOTE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_RUN_WORDS = 6

_CELLS = {
    ("project", "every_turn"): "project context/",
    ("project", "task_start"): "project skills/",
    ("project", "lookup"): "shared/<category>/",
    ("everywhere", "every_turn"): "workspace context/50-USER-PROFILE.md",
    ("everywhere", "task_start"): "workspace skills/",
    ("everywhere", "lookup"): "workspace USER.md",
}


@dataclass(frozen=True)
class ProposalResult:
    """``outcome`` is ``proposed``, ``deferred``, ``unplaced`` or ``refused``."""

    outcome: str
    reason: str
    url: str | None = None


def marker(thread_id: str, entry_id: int, revision: int) -> str:
    return f"Placement: {thread_id}/{entry_id}@{revision}"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def shares_run(candidate: str, source: str, run: int = _RUN_WORDS) -> bool:
    """Whether ``candidate`` repeats a source run, up to ``run`` words."""
    mine = _tokens(candidate)
    if not mine:
        return False
    run = min(run, len(mine))
    words = _tokens(source)
    seen = {tuple(words[i:i + run]) for i in range(len(words) - run + 1)}
    return any(tuple(mine[i:i + run]) in seen for i in range(len(mine) - run + 1))


def thread_user_names(db: sqlite3.Connection, thread_id: str) -> list[str]:
    """The names the screen keeps out of a publication: the thread's user.

    The one lookup staging and the worker share, so they cannot disagree on who
    is known. A thread with no user id (NULL or empty) names nobody: reading it
    as the text ``None`` would refuse any lesson that says "none".
    """
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return [str(row[0])] if row is not None and row[0] else []


def p3_violation(text: str, entry_text: str, user_names: list[str]) -> bool:
    """The widening rule: rephrased, and naming nobody."""
    return p3_problem(text, entry_text, user_names) is not None


def p3_problem(text: str, entry_text: str, user_names: list[str]) -> str | None:
    """Why ``text`` may not be published, said so its author can rephrase.

    The one name screen for everything placement publishes (#2668). A name
    matches in any form (``Alex's``, ``Alex,``): ``user_names`` in any case,
    and each word of a person the source names (see ``_named_people``) when
    the candidate capitalizes it too, so a surname or a third party is refused
    as well as the user.
    """
    if shares_run(text, entry_text):
        return f"it repeats {_RUN_WORDS} or more words of the lesson in a row"
    named = names_in(text, entry_text, user_names)
    if named:
        return f"it names {', '.join(sorted(named))}"
    return None


def names_in(text: str, entry_text: str, user_names: list[str]) -> set[str]:
    """The person-name words ``text`` carries, by the rule ``p3_problem`` applies."""
    words = {_bare(word) for word in _WORD.findall(text)}
    lowered = {word.lower() for word in words}
    return {
        part for name in user_names for part in _tokens(name)
        if len(part) >= 3 and part in lowered
    } | (_named_people(entry_text) & words)


_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
_CAPITALIZED = re.compile(r"[A-Z][a-z]+(?:['’][A-Za-z]+)*")
_SENTENCES = re.compile(r"(?<=[.!?;:])\s+|\n+")
#: Closed-class words a sentence may open with; their capital is grammar.
_OPENERS = frozenset(
    "a an the this that these those it its our your their my his her we you they "
    "if when while after before once since until unless because though where why "
    "how what which who and but or so then also for in on at to from with by of "
    "as every each all any some no not only never always".split()
)


def _bare(word: str) -> str:
    """The word without a possessive or contraction tail."""
    return re.split(r"['’]", word, maxsplit=1)[0]


def _named_people(source: str) -> set[str]:
    """Each word, three letters or more, of a name-shaped run in ``source``.

    A run is two or more adjacent capitalized words within one sentence (the
    caller gives one field per line). Capitals that grammar or a heading
    forced name nobody: a sentence's first word is never a name, though it
    still joins a run (``Dana Whitfield approved`` names ``Whitfield``), a
    closed-class opener (``The``, ``When``) joins nothing, and a heading
    (title case, no closing punctuation) is skipped. A product name mid-
    sentence (``Claude Code``) reads as a person; staging refuses it with the
    words to drop, so the author rephrases instead of the lesson being lost.
    """
    names: set[str] = set()
    for sentence in _SENTENCES.split(source):
        words = [_bare(word) for word in _WORD.findall(sentence)]
        capital = [bool(_CAPITALIZED.fullmatch(word)) for word in words]
        if _heading(sentence, words, capital):
            continue
        if words and words[0].lower() in _OPENERS:
            capital[0] = False
        run: list[tuple[int, str]] = []
        for index, (word, up) in enumerate([*zip(words, capital), ("", False)]):
            if up:
                run.append((index, word))
                continue
            if len(run) >= 2:
                names.update(
                    part for at, part in run if at > 0 and len(part) >= 3
                )
            run = []
    return names


def _heading(sentence: str, words: list[str], capital: list[bool]) -> bool:
    """Title case with no closing punctuation, as a lesson title is written."""
    return (
        len(words) >= 3
        and not sentence.rstrip().endswith((".", "!", "?"))
        and all(up for word, up in zip(words, capital) if len(word) >= 4)
    )


def resolvable_ref(db: sqlite3.Connection, thread_id: str, refs: list[dict]) -> bool:
    """A wider audience needs a source a reviewer can open, not a description."""
    for ref in refs:
        kind, value = ref.get("kind"), str(ref.get("reference") or "").strip()
        if kind == "message" and db.execute(
            "SELECT 1 FROM messages WHERE thread_id=? AND id=?", (thread_id, value)
        ).fetchone():
            return True
        if kind == "turn" and db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id=? AND id=?", (thread_id, value)
        ).fetchone():
            return True
        if kind == "file" and value and _workspace_path(value).is_file():
            return True
    return False


def _workspace_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    from infra.config import WORKSPACE

    return Path(WORKSPACE) / path


def backlog_target(
    db: sqlite3.Connection, project_id: str | None
) -> tuple[str | None, str | None]:
    """The conductor's lane label and the project's GitHub repository."""
    from infra.conductor import get_scope_coordinator

    conductor = get_scope_coordinator(project_id or "workspace", db=db)
    label = None
    repo_project = project_id
    if conductor:
        row = db.execute(
            "SELECT agent_notes, project_id FROM threads WHERE id=?", (conductor,)
        ).fetchone()
        if row is not None:
            match = _LANE.search(str(row["agent_notes"] or ""))
            label = match.group(1) if match else None
            repo_project = repo_project or row["project_id"]
    repo = None
    if repo_project:
        from infra.project_repo_policy import get_project_repo_policies

        for policy in get_project_repo_policies(repo_project, db=db).values():
            repo = _github_repo(Path(policy.path))
            if repo:
                break
    return label, repo


def _github_repo(path: Path) -> str | None:
    import subprocess

    from infra.backend_git import git_argv

    result = subprocess.run(
        git_argv("-C", str(path), "remote", "get-url", "origin"),
        capture_output=True, text=True, timeout=10, check=False,
    )
    match = _REMOTE.search(result.stdout.strip())
    return match.group(1) if match else None


def file_issue(
    *,
    repo: str,
    label: str,
    thread_id: str,
    entry_id: int,
    revision: int,
    q1: str,
    q2: str,
    rule: str,
    rationale: str,
) -> ProposalResult:
    from reviewer.gh_api import run_gh

    mark = marker(thread_id, entry_id, revision)
    found = run_gh([
        "issue", "list", "--repo", repo, "--state", "all", "--limit", "5",
        "--search", f'"{mark}" in:body', "--json", "url,body",
    ])
    if found.returncode != 0:
        return ProposalResult("deferred", "deferred-github-unavailable")
    for issue in json.loads(found.stdout or "[]"):
        if mark in str(issue.get("body") or ""):
            return ProposalResult("proposed", "proposed-existing", issue["url"])
    destination = _CELLS[(q1, q2)]
    prefix = "[workspace] " if q1 == "everywhere" else ""
    body = (
        f"Proposed rule for `{destination}`:\n\n> {rule}\n\n"
        f"Why this destination: {rationale}\n\n"
        f"Source: thread `{thread_id}` continuity entry {entry_id}@{revision}. "
        "Verify its provenance before accepting.\n\n"
        f"{mark}\n"
    )
    created = run_gh(
        [
            "issue", "create", "--repo", repo, "--label", label,
            "--title", f"{prefix}Proposed rule for {destination}",
            "--body-file", "-",
        ],
        input_text=body,
    )
    if created.returncode != 0:
        return ProposalResult("deferred", "deferred-github-unavailable")
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else None
    return ProposalResult("proposed", "proposed", url)
