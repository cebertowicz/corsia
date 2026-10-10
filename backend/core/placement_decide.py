"""Where one selected lesson belongs, answered by the checkpoint fork, mapped by us.

The fork answers only the two questions of the ``memory-placement`` skill
(who must have it, and when) and drafts the line. It never names a path: the
backend maps the answer onto the skill's table, so a model can never write
outside the thread's own tray or its agent's reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.lesson_trials import TRIAL_SCHEMA, Trial, validate_trial

Q1 = ("thread", "agent", "project", "everywhere")
Q2 = ("every_turn", "task_start", "lookup")

#: WORKSPACE-STANDARD.md §Caps. A non-agent thread's context files take the
#: RESPONSIBILITY.md row (Context & Memory, 2026-09-24).
CONTEXT_WORDS = 700
CONTEXT_LESSONS = 5
SKILL_BODY_WORDS = 800
SKILL_DESCRIPTION_WORDS = 60

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

GUIDE = """Place the lesson where its next reader will already look. Answer \
two questions.

q1, who must have it:
- thread: only this thread's current work.
- agent: this agent's future work.
- project: everyone working on this project.
- everywhere: every project; how the user works in general.
Scope words decide: "here", "this time", "for this report" mean thread;
"always", "every project", "everyone" widen it.

q2, when they must have it:
- every_turn: it changes a first decision on nearly every turn (a rule or preference).
- task_start: a procedure for a recognizable kind of task.
- lookup: a fact or reference.

Then:
- duplicate_of: if a destination text already says it, quote that exact line;
  otherwise null.
- text: the one line to add, as a rule in the destination's voice. For project
  or everywhere, write an impersonal rule: no quote, no person's name, no
  private detail, no mention of the incident.
- skill_name: for agent + task_start, an existing skill name from the list or a
  new lowercase-hyphenated name; otherwise null.
- skill_description: only for a new skill, when to use it in at most 60 words;
  otherwise null.
- reference_file: for agent + lookup, one file name from the reference list;
  otherwise null.
- rationale: one short line on why this destination.
- trial: only for your own unproven hypothesis about how to work, to try as a
  skill until a review date; otherwise leave it out. Never for what a person
  told you: an instruction, preference or constraint keeps its permanent home.
  With it: q1 agent, kind hypothesis, review_after (a future date), a new
  skill_name, skill_description (when it applies, at most 40 words) and
  boundary (when it does not). If it rests on a person's words, add derivation
  (how the rule follows) and literal_ref (where their exact words are kept)."""

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "q1", "q2", "duplicate_of", "text", "skill_name", "skill_description",
        "reference_file", "rationale",
    ],
    "properties": {
        "q1": {"type": "string", "enum": list(Q1)},
        "q2": {"type": "string", "enum": list(Q2)},
        "duplicate_of": {"type": ["string", "null"]},
        "text": {"type": "string"},
        "skill_name": {"type": ["string", "null"]},
        "skill_description": {"type": ["string", "null"]},
        "reference_file": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
        "trial": TRIAL_SCHEMA,
    },
}


class InvalidDecision(ValueError):
    """The model's answer does not satisfy the contract."""


@dataclass(frozen=True)
class Decision:
    q1: str
    q2: str
    duplicate_of: str | None
    text: str
    skill_name: str | None
    skill_description: str | None
    reference_file: str | None
    rationale: str
    trial: Trial | None = None


def _optional(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidDecision(f"{key} must be a string or null")
    value = value.strip()
    return value or None


def validate_decision(raw: object) -> Decision:
    """Refuse any placement answer outside the contract."""
    if not isinstance(raw, dict):
        raise InvalidDecision("placement answer must be an object")
    if raw.get("q1") not in Q1 or raw.get("q2") not in Q2:
        raise InvalidDecision("q1 or q2 is outside the table")
    text = raw.get("text")
    rationale = raw.get("rationale")
    if not isinstance(text, str) or not isinstance(rationale, str):
        raise InvalidDecision("text and rationale must be strings")
    text = " ".join(text.split())
    if not text or len(text) > 600:
        raise InvalidDecision("text must be one non-empty line of at most 600 characters")
    try:
        trial = validate_trial(raw.get("trial"))
    except ValueError as invalid:
        raise InvalidDecision(str(invalid)) from invalid
    return Decision(
        q1=str(raw["q1"]),
        q2=str(raw["q2"]),
        duplicate_of=_optional(raw, "duplicate_of"),
        text=text,
        skill_name=_optional(raw, "skill_name"),
        skill_description=_optional(raw, "skill_description"),
        reference_file=_optional(raw, "reference_file"),
        rationale=" ".join(rationale.split())[:200],
        trial=trial,
    )


@dataclass(frozen=True)
class ThreadPlace:
    """What placement knows about the lesson's thread and its destinations."""

    thread_id: str
    title: str
    project_id: str | None
    agent: bool
    tray: Path | None
    reference_dir: Path | None


@dataclass(frozen=True)
class Destination:
    """``kind`` is ``stays``, ``context``, ``skill``, ``reference``, ``proposal`` or ``trial``."""

    kind: str
    path: Path | None = None
    reason: str = ""


def context_file(place: ThreadPlace) -> Path | None:
    """The thread's every-turn bootloader file."""
    if place.tray is None:
        return None
    context = place.tray / "context"
    responsibility = context / "RESPONSIBILITY.md"
    if place.agent or responsibility.exists():
        return responsibility
    return context / "LESSONS.md"


def map_destination(place: ThreadPlace, decision: Decision) -> Destination:
    """Read one cell of the memory-placement table."""
    if decision.trial is not None:
        return _trial_destination(place, decision)
    if decision.q1 in ("project", "everywhere"):
        return Destination("proposal")
    if decision.q1 == "thread":
        return Destination("stays", reason="thread-scope")
    if place.tray is None:
        return Destination("stays", reason="no-thread-tray")
    if decision.q2 == "every_turn":
        return Destination("context", context_file(place))
    if decision.q2 == "task_start":
        name = decision.skill_name or ""
        if not _SLUG.match(name) or len(name) > 64:
            raise InvalidDecision("skill_name must be a lowercase-hyphenated name")
        return Destination("skill", place.tray / "skills" / name / "SKILL.md")
    if place.reference_dir is None:
        return Destination("stays", reason="no-agent-reference")
    name = decision.reference_file or ""
    candidate = place.reference_dir / name
    if (
        not name or "/" in name or not name.endswith(".md")
        or not candidate.is_file()
    ):
        return Destination("stays", reason="no-agent-reference")
    return Destination("reference", candidate)


def _trial_destination(place: ThreadPlace, decision: Decision) -> Destination:
    """A trial is always its owner's own skill, whatever ``q2`` says.

    The path is None when no usable name came: a hypothesis that is only
    captured needs none.
    """
    if decision.q1 != "agent":
        raise InvalidDecision(
            "a trial is your own hypothesis about how you work: q1 must be agent. "
            "A project or everywhere lesson is proposed, without trial"
        )
    if place.tray is None:
        return Destination("stays", reason="no-thread-tray")
    name = decision.skill_name or ""
    if not _SLUG.match(name) or len(name) > 64:
        return Destination("trial")
    return Destination("trial", place.tray / "skills" / name / "SKILL.md")


def destination_texts(place: ThreadPlace) -> dict[str, Any]:
    """The candidate destinations' current text, bounded, for the review block."""
    texts: dict[str, Any] = {}
    context = context_file(place)
    if context is not None and context.exists():
        texts["bootloader"] = context.read_text()[:12_000]
    skills = []
    if place.tray is not None and (place.tray / "skills").is_dir():
        for skill in sorted((place.tray / "skills").glob("*/SKILL.md"))[:20]:
            skills.append({
                "name": skill.parent.name,
                "description": skill_description(skill.read_text()) or "",
            })
    texts["skills"] = skills
    if place.reference_dir is not None and place.reference_dir.is_dir():
        texts["reference_files"] = sorted(
            path.name for path in place.reference_dir.glob("*.md")
        )[:40]
    return texts


def words(text: str) -> int:
    """Word count, as WORKSPACE-STANDARD's caps measure it."""
    return len(text.split())


def squash(text: str) -> str:
    """Whitespace-folded lower case, for finding a quoted passage in a file."""
    return " ".join(text.split()).lower()


#: A quote shorter than this proves nothing: it matches almost any file.
MIN_QUOTE = 12


def says(text: str, quote: str) -> bool:
    """Whether ``text`` carries ``quote``, ignoring list markers and spacing.

    The one test of "this destination says the lesson": an in-turn place, a
    fork's ``already``, the worker's duplicate and fold checks, and a make-room
    basis all decide with it.
    """
    needle = squash(quote.lstrip("-* "))
    return len(needle) >= MIN_QUOTE and needle in squash(text)


def skill_body(text: str) -> str:
    """A skill without its leading frontmatter, the part its body cap counts."""
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)


def lesson_count(text: str) -> int:
    """List items under every ``## Lessons`` heading."""
    inside, count = False, 0
    for line in text.split("\n"):
        if line.startswith("## "):
            inside = bool(re.match(r"^## Lessons\s*$", line))
        elif inside and line.lstrip().startswith("- "):
            count += 1
    return count


def over_cap(kind: str, text: str) -> bool:
    """Whether a context file or skill passes its WORKSPACE-STANDARD cap.

    The one measure for both questions the pass asks: does this write need
    room, and does the file fit once room is made.
    """
    if kind == "skill":
        return words(skill_body(text)) > skill_body_cap(text)
    if kind == "context":
        return words(text) > CONTEXT_WORDS or lesson_count(text) > CONTEXT_LESSONS
    return False


def skill_body_cap(text: str) -> int:
    """The body cap the skill loader enforces: a trial lesson's when it is one.

    A lesson skill that passed the standard's cap but not the lesson's would
    be refused at save, so the fork must be asked for room against the same
    number the loader checks.
    """
    from core.lesson_trials import BODY_WORDS, lesson_block
    from core.skill_validation import validate_skill_text

    if lesson_block(validate_skill_text(text).metadata) is not None:
        return BODY_WORDS
    return SKILL_BODY_WORDS


def skill_description(text: str) -> str | None:
    """A SKILL.md's parsed frontmatter description, if it has one."""
    from core.skill_validation import validate_skill_text

    return validate_skill_text(text).description


def append_lesson(text: str | None, line: str, day: str) -> tuple[str, int]:
    """Add one dated lesson under ``## Lessons``; return the text and lesson count."""
    body = text if text is not None else "# Lessons\n"
    lesson = f"- {day}: {line}"
    if not re.search(r"^## Lessons\s*$", body, re.MULTILINE):
        body = body.rstrip("\n") + "\n\n## Lessons\n"
    lines = body.rstrip("\n").split("\n")
    start = next(i for i, row in enumerate(lines) if re.match(r"^## Lessons\s*$", row))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, lesson)
    result = "\n".join(lines) + "\n"
    return result, lesson_count(result)


def append_to_skill(
    text: str | None, name: str, line: str, description: str | None
) -> str:
    """Add one line to a skill, creating it with frontmatter when new."""
    if text is None:
        from core.skill_validation import skill_template

        template = skill_template(name, description or line)
        frontmatter, _title, _placeholder = template.partition(f"# {name}\n")
        return frontmatter.rstrip("\n") + f"\n\n# {name}\n\n- {line}\n"
    return text.rstrip("\n") + f"\n- {line}\n"


def append_to_reference(text: str, line: str) -> str:
    """Add one line to the end of an agent reference file."""
    return text.rstrip("\n") + f"\n\n- {line}\n"
