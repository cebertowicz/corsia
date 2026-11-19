"""What the fresh reader is told, what it is given, and the only answer shapes accepted.

The reader sees the item exactly as the person will, the documents it hands over, the
task it names, and the person's own messages (newest first, as many as the input cap
leaves room for). It never sees the writer's transcript. It may fail an item only by
quoting a line of it and naming which of four checks the line breaks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SYSTEM_PROMPT = """You check one item an agent is about to put in front of a person, before the person sees it. \
You did not write it. Read it only against the person's own words and the task supplied with it.

Fail it only for one of these, quoting the exact line at fault from the item:
1. The person can't act on it from what it shows: for example, a choice that leaves out what each option \
costs or changes; a step that doesn't say what to do; a line that needs another document to make sense.
2. It asks or reopens something the supplied words already settle.
3. It changes what the person asked for: it adds, drops or swaps a part they stated.
4. It doesn't say whether a claim is how things are, a plan, or already done.

Anything else passes. A gap in what you were given is not a failure. The writer may add a note answering an \
earlier finding; you still decide.

Reply with one JSON object and nothing else:
{"verdict": "pass" or "fail", "findings": [{"quote": "<exact text copied from the item>", "check": <1-4>, \
"why": "<one line>"}]}
A pass has no findings. A fail has at least one."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "check": {"type": "integer", "enum": [1, 2, 3, 4]},
                    "why": {"type": "string"},
                },
                "required": ["quote", "check", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReaderAnswer:
    passed: bool
    findings: tuple[dict[str, Any], ...]


def _answer_object(raw: object) -> object:
    """A harness hands a parsed object; Native hands raw text, maybe fenced or with reasoning tags."""
    if not isinstance(raw, str):
        return raw
    from llm.isolated_structured import _strict_json_loads
    from llm.think_tags import strip_think_tags

    text = (strip_think_tags(raw) or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return _strict_json_loads(text)


def validator(shown: str):
    """Accept only a well-formed verdict whose every finding quotes the shown text exactly.

    Anything else raises, so the read counts as that candidate failing and the next one is
    tried; a malformed answer never delivers and never fails the item on its own.
    """
    def validate(raw: object) -> ReaderAnswer:
        data = _answer_object(raw)
        if not isinstance(data, dict) or data.get("verdict") not in ("pass", "fail"):
            raise ValueError("reader answer has no pass/fail verdict")
        findings = data.get("findings")
        if not isinstance(findings, list):
            raise ValueError("reader answer has no findings list")
        kept = []
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("check") not in (1, 2, 3, 4):
                raise ValueError("reader finding names no check")
            quote = str(finding.get("quote") or "").strip()
            if not quote or quote not in shown:
                raise ValueError("reader finding does not quote the item")
            kept.append({"quote": quote, "check": finding["check"], "why": str(finding.get("why") or "").strip()})
        if data["verdict"] == "fail" and not kept:
            raise ValueError("reader failed the item without a finding")
        return ReaderAnswer(passed=data["verdict"] == "pass", findings=tuple(kept) if data["verdict"] == "fail" else ())
    return validate


@dataclass(frozen=True)
class Context:
    """What the reader gets besides the item; ``messages`` are (created_at, text), newest first."""

    task: str | None
    messages: tuple[tuple[str, str], ...]
    note_to_reader: str | None
    where: str | None = None


def request_text(user: str) -> str:
    """The request's messages as sent, the text the input cap is measured on.

    Its JSON role wrappers and escapes run longer than a chat template's own role
    markers, so measuring this text bounds what the reader's model is given.
    """
    return json.dumps([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                      ensure_ascii=False)


def _as_sent(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


_NO_CAP = "none: no input cap is known"
_NONE_FIT = "none: none fit"


def _assemble(fixed: str, lines: list[str], cut_from: str | None) -> str:
    title = "## The person's own messages, newest first"
    if cut_from is not None:
        title += f" (cut: {cut_from})" if cut_from.startswith("none") else f" (cut: you have them only from {cut_from} on)"
    return f"{fixed}\n\n{title}\n" + ("\n".join(lines) if lines else "(none)")


def user_prompt(kind: str, shown: str, documents: tuple[tuple[str, str], ...], context: Context,
                cap: int | None, measure) -> tuple[str, str | None]:
    """The reader's input, and the date its person messages start from when the cap cut them.

    ``cap`` bounds the whole request (:func:`request_text`) under ``measure``. The item, its
    documents and the task always go in full; the person's messages fill what is left,
    newest first. With no cap known, no message goes in and the reader is told so.
    """
    head = [f"## The item ({kind}), exactly as the person will see it\n{shown}"]
    if context.where:
        head.append(f"## Where it sits\n{context.where}")
    for name, text in documents:
        head.append(f"## Document it hands over: {name}\n{text}")
    if context.task:
        head.append(f"## The task it names\n{context.task}")
    if context.note_to_reader:
        head.append(f"## The writer's note to you (the person never sees it)\n{context.note_to_reader}")
    fixed = "\n\n".join(head)
    if cap is None:
        return _assemble(fixed, [], _NO_CAP), _NO_CAP
    # The title's longest cut note is held back before any message is placed.
    longest = max((len(created_at) for created_at, _text in context.messages), default=0)
    room = cap - max(measure(request_text(_assemble(fixed, [], "0" * longest))),
                     measure(request_text(_assemble(fixed, [], _NONE_FIT))))
    placed: list[tuple[str, str]] = []
    for created_at, text in context.messages:
        line = message_line(created_at, text)
        cost = measure(_as_sent(line + "\n"))
        if cost > room:
            break
        room -= cost
        placed.append((created_at, line))
    while True:  # the whole request is measured once more; a count that is not additive drops the oldest
        cut_from = None
        if len(placed) < len(context.messages):
            cut_from = placed[-1][0] if placed else _NONE_FIT
        prompt = _assemble(fixed, [line for _at, line in placed], cut_from)
        if not placed or measure(request_text(prompt)) <= cap:
            return prompt, cut_from
        placed.pop()


def message_line(created_at: str, text: str) -> str:
    """One of the person's messages as the reader gets it; its measure is what it costs the cap."""
    return f"- {created_at}: {text}"


def task_text(intent: dict[str, Any] | None) -> str | None:
    if not intent:
        return None
    return json.dumps({key: intent.get(key) for key in ("title", "intent", "acceptance_criteria")}, ensure_ascii=False)
