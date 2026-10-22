"""Pure task-term extraction and window matching shared by task-context readers.

The module has no database or delivery dependency: callers supply task text and
candidate words, then use the best matching window when it clears the floor.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

MAX_TERMS = 8
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-]{2,}")
_STOPWORDS = frozenset("""
    about above after again against all also and any are because been before
    being below between both but can could did does doing done down during each
    either else every few for from further had has have having here how into
    its itself just like made make many may more most must need needs new not
    now off once only other our out over own per same see should since some such
    than that the their them then there these they this those through too
    under until upon use used using very via was were what when where which
    while who whom why will with within without would yes you your
    add adds added change changes fix fixes fixed update updates updated
    task tasks node nodes plan step steps work item items pass passes passing
    passed test tests tested green done result results evidence criteria
    acceptance record recorded show shows shown one two three
""".split())


@dataclass(frozen=True)
class NodeText:
    """The node reading used to find earlier matching words."""

    title: str
    intent: str = ""
    criteria: str = ""
    words: str = ""
    node_id: str | None = None


@dataclass(frozen=True)
class TaskTerms:
    """Distinct task terms and the subset originating in the title."""

    terms: tuple[str, ...]
    title_terms: frozenset[str]


def _distinct(tokens: list[str]) -> list[str]:
    """Drop stopwords and bare numbers shorter than an issue number, keep order."""
    seen: dict[str, None] = {}
    for token in tokens:
        token = token.strip("-_")
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token.isdigit() and len(token) < 4:
            continue
        seen.setdefault(token, None)
    return list(seen)


def task_terms(text: NodeText) -> TaskTerms:
    """Return today's distinctive-term rule, with title terms first."""
    title = _distinct(_TOKEN.findall((text.title or "").lower()))
    rest = _distinct(_TOKEN.findall(
        f"{text.intent or ''} {text.criteria or ''} {text.words or ''}".lower()
    ))
    chosen = tuple((title + [term for term in rest if term not in title])[:MAX_TERMS])
    return TaskTerms(chosen, frozenset(term for term in title if term in chosen))


def floor(term_count: int) -> int:
    """Distinct terms a hit must share: two, or a third of the terms if more."""
    return max(2, math.ceil(term_count / 3))


@dataclass(frozen=True)
class Match:
    """The best matching window and the coverage that selected it."""

    shared: int
    title_shared: int
    matched_terms: frozenset[str]
    span: str


def _windows(words: str) -> list[str]:
    """Return paragraphs, splitting long ones into sentence runs no wider than 400 chars."""
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", words) if paragraph.strip()]
    windows: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= 400:
            windows.append(paragraph)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        run = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if run and len(run) + 1 + len(sentence) > 400:
                windows.append(run)
                run = sentence
            elif not run:
                run = sentence
            else:
                run += " " + sentence
        if run:
            windows.append(run)
    return windows


def first_term_at(terms: frozenset[str], text: str) -> int:
    """Offset of the first word in ``text`` that ``match()`` counts for one of ``terms``, else 0."""
    for token in _TOKEN.finditer(text.lower()):
        if any(token.group().startswith(term) for term in terms):
            return token.start()
    return 0


def match(terms: TaskTerms, words: str) -> Match | None:
    """Return the highest-coverage matching window, or ``None`` below today's floor."""
    if len(terms.terms) < 2 or not terms.title_terms:
        return None
    # A window's matched terms all appear somewhere in the text, so this skips most rows cheaply.
    lowered = words.lower()
    present = {term for term in terms.terms if term in lowered}
    if len(present) < floor(len(terms.terms)) or terms.title_terms.isdisjoint(present):
        return None
    best: Match | None = None
    for window in _windows(words):
        tokens = set(_TOKEN.findall(window.lower()))
        matched = frozenset(
            term for term in terms.terms if any(token.startswith(term) for token in tokens)
        )
        candidate = Match(
            shared=len(matched),
            title_shared=len(matched & terms.title_terms),
            matched_terms=matched,
            span=window,
        )
        if best is None or (candidate.shared, candidate.title_shared) > (best.shared, best.title_shared):
            best = candidate
    if best is None or best.shared < floor(len(terms.terms)) or best.title_shared < 1:
        return None
    return best
