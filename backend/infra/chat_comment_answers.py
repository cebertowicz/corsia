"""Place tagged answers when a message is written."""

from __future__ import annotations

import json
import re
import sqlite3

from infra.chat_comment_ids import CID_PATTERN, parse_cid

_TAGGED = re.compile(rf"(?:\A|\r?\n[ \t]*\r?\n)([ \t]*(?:\[{CID_PATTERN}\][ \t]*)+)")
_TAG = re.compile(rf"\[({CID_PATTERN})\]")
# Hold a possible section boundary, including an incomplete first newline.
_PARTIAL_TAG = re.compile(
    r"(?:\A|\r?\n[ \t]*\r?\n)[ \t]*(?:\[(?:c(?:[1-9][0-9]*(?:\.(?:[1-9][0-9]*)?)?)?)?)?$"
    r"|\r?\n[ \t]*\r?$|\r$"
)


class VisibleAnswerStream:
    """Hold a possible answer until the saved row establishes its placement."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pending: list[str] = []
        self.partial_state = ""
        self.started = False
        self.held = False

    def _extend_partial(self, chunk: str) -> bool:
        state = self.partial_state
        for char in chunk:
            if state in ("newline", "boundary") and char in " \t":
                continue
            if state == "newline" and char == "\r":
                state = "cr"
            elif state == "bracket" and char == "c":
                state = "c"
            elif state in ("c", "dot") and char in "123456789":
                state = "number" if state == "c" else "subnumber"
            elif state in ("number", "subnumber") and char in "0123456789":
                continue
            elif state == "number" and char == ".":
                state = "dot"
            else:
                return False
        self.partial_state = state
        return True

    def feed(self, chunk: str) -> str:
        if self.held:
            return ""
        if self.pending and self._extend_partial(chunk):
            self.pending.append(chunk)
            return ""
        # A sentinel preserves the true-start anchor after visible text is sent.
        prefix = "x" if self.started else ""
        text = prefix + ''.join(self.pending) + chunk
        tag = _TAGGED.search(text, len(prefix))
        partial = _PARTIAL_TAG.search(text, len(prefix))
        end = min((m.start() for m in (tag, partial) if m), default=len(text))
        self.held = tag is not None and tag.start() == end
        self.pending = [] if self.held or end == len(text) else [text[end:]]
        if self.pending and partial is not None and partial.end() == len(text):
            suffix = self.pending[0]
            if '[' in suffix:
                cid = suffix.rsplit('[', 1)[1]
                self.partial_state = ('bracket' if not cid else 'c' if cid == 'c' else
                                      'dot' if cid.endswith('.') else
                                      'subnumber' if '.' in cid else 'number')
            else:
                self.partial_state = ('cr' if suffix.endswith('\r') else
                                      'newline' if re.fullmatch(r'\r?\n[ \t]*', suffix) else 'boundary')
        else:
            self.partial_state = ''
        visible = text[len(prefix):end]
        self.started = self.started or bool(visible)
        return visible


def parse_answers(text: str, message_id: str) -> dict[str, list[dict[str, str]]]:
    """Return each tagged section for every comment it names."""
    matches = list(_TAGGED.finditer(text))
    answers: dict[str, list[dict[str, str]]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        answer = text[match.end():end].strip()
        for cid in dict.fromkeys(_TAG.findall(match.group(1))):
            answers.setdefault(cid, []).append({"text": answer, "assistant_message_id": message_id})
    return answers


def visible_answer_content(text: str, placed_cids: list[str]) -> str:
    """Keep the introduction and sections whose tags were not placed."""
    if not placed_cids:
        return text
    matches = []
    pos = 0
    while text.find('[c', pos) != -1:
        match = _TAGGED.search(text, pos)
        if match is None:
            break
        matches.append(match)
        pos = match.end()
    result = text[:matches[0].start()] if matches else text
    known = set(placed_cids)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if not known.intersection(_TAG.findall(match.group(1))):
            result += text[match.start():end]
    return result.strip()


def project_saved_answers(db: sqlite3.Connection, rows: list[tuple[str, str | None, str | None]]) -> list[dict]:
    """Project placed answers for saved rows with one shared batch lookup."""
    pairs = list(dict.fromkeys((thread, message) for thread, message, text in rows
                               if message and text and '[c' in text))
    answers: dict[tuple[str, str], list[str]] = {}
    if pairs:
        query = ("SELECT a.thread_id,a.assistant_message_id,a.cid "
                 "FROM json_each(?) requested CROSS JOIN chat_comment_answers a "
                 "ON a.assistant_message_id=json_extract(requested.value, '$[1]') "
                 "AND a.thread_id=json_extract(requested.value, '$[0]') "
                 + SHOWN_ANSWERS_JOINS)
        for thread, message, cid in db.execute(query, (json.dumps(pairs),)):
            answers.setdefault((thread, message), []).append(cid)
    result = []
    for thread, message, text in rows:
        cids = answers.get((thread, message), []) if text and '[c' in text else []
        result.append({'chat_answer_cids': cids, 'chat_visible_content': visible_answer_content(text, cids)}
                      if cids and text is not None else {})
    return result


def strip_answer_tags(text: str | None, limit: int | None = None) -> str | None:
    """Drop answer tags, optionally stopping after the visible prefix."""
    if not text:
        return text
    chunks: list[str] = []
    pos = length = 0
    while limit is None or length < limit:
        search_end = len(text) if limit is None else pos + limit - length + 1
        if text.find('[c', pos, search_end) == -1:
            break
        match = _TAGGED.search(text, pos)
        if match is None:
            break
        prefix = text[pos:match.start(1)]
        chunks.append(prefix)
        length += len(prefix)
        pos = match.end()
    chunks.append(text[pos:] if limit is None else text[pos:pos + max(0, limit - length)])
    result = ''.join(chunks)
    return result if limit is None else result[:limit]


def project_previews(db: sqlite3.Connection, rows: list[tuple[str, str | None, str | None]],
                     *, limit: int | None = 100) -> list[str | None]:
    """Project saved content before stripping tags for previews or summaries."""
    projections = project_saved_answers(db, rows)
    return [strip_answer_tags(projection.get('chat_visible_content', text), limit)
            for (_, _, text), projection in zip(rows, projections)]


def has_comment_cid(metadata: str | None, cid: str) -> bool:
    try:
        value = json.loads(metadata or "{}")
        comments = value.get("reply_context", {}).get("comments", [])
        return isinstance(comments, list) and any(
            isinstance(item, dict) and item.get("cid") == cid for item in comments
        )
    except (ValueError, TypeError, AttributeError):
        return False


def place_answers(
    db: sqlite3.Connection, message_id: str, thread_id: str, role: str, content: str, seq: int, created_at: str,
) -> None:
    """Index known comments in the same transaction as the assistant message."""
    if role != "assistant" or "[c" not in content:
        return
    for cid in parse_answers(content, message_id):
        parsed = parse_cid(cid)
        if parsed is None:
            continue
        row = db.execute(
            "SELECT id, metadata, seq FROM messages WHERE thread_id=? AND input_order=? AND role='user'",
            (thread_id, parsed[0]),
        ).fetchone()
        if row is None or row[2] is None or row[2] >= seq:
            continue
        if has_comment_cid(row[1], cid):
            db.execute(
                "INSERT OR IGNORE INTO chat_comment_answers VALUES (?,?,?,?,?)",
                (thread_id, row[0], cid, message_id, created_at),
            )


def delete_thread_chat_comment_index_rows(db: sqlite3.Connection, thread_id: str) -> None:
    """Remove derived rows before the thread's messages are deleted."""
    db.execute(
        "DELETE FROM chat_comment_views WHERE user_message_id IN "
        "(SELECT id FROM messages WHERE thread_id=?)", (thread_id,),
    )
    db.execute("DELETE FROM chat_comment_answers WHERE thread_id=?", (thread_id,))
    db.execute("DELETE FROM chat_comments WHERE thread_id=?", (thread_id,))


# All projections compose this source and predicate so that missing comments,
# messages, and viewed answers have the same meaning everywhere.
# An answer shows in its comment thread only while its comment, sender and target rows exist.
SHOWN_ANSWERS_JOINS = """JOIN chat_comments c ON c.user_message_id=a.user_message_id AND c.cid=a.cid AND c.thread_id=a.thread_id
JOIN messages sender ON sender.id=a.user_message_id AND sender.thread_id=a.thread_id
JOIN messages target ON target.id=c.target_message_id AND target.thread_id=a.thread_id"""
SHOWN_ANSWERS_FROM = "FROM chat_comment_answers a " + SHOWN_ANSWERS_JOINS
UNREAD_FROM = """FROM chat_comment_answers a
JOIN threads t ON t.id=a.thread_id AND t.user_id=?
JOIN chat_comments c ON c.thread_id=a.thread_id AND c.user_message_id=a.user_message_id AND c.cid=a.cid
JOIN messages target ON target.id=c.target_message_id AND target.thread_id=a.thread_id
JOIN messages u ON u.id=a.user_message_id AND u.thread_id=a.thread_id
JOIN messages m ON m.id=a.assistant_message_id AND m.thread_id=a.thread_id
LEFT JOIN chat_comment_views v ON v.user_id=? AND v.user_message_id=a.user_message_id AND v.cid=a.cid"""
# One agent message is one answer in a comment thread, however many of the thread's cids it tags.
# That answer is read and stamped through its first row: the thread's earliest tagged comment.
UNREAD_WHERE = """(v.user_id IS NULL OR v.seen_at < a.answered_at) AND NOT EXISTS (
SELECT 1 FROM chat_comment_answers o
JOIN chat_comments oc ON oc.thread_id=o.thread_id AND oc.user_message_id=o.user_message_id AND oc.cid=o.cid
WHERE o.assistant_message_id=a.assistant_message_id AND +o.thread_id=a.thread_id
AND COALESCE(oc.root_user_message_id,oc.user_message_id)=COALESCE(c.root_user_message_id,c.user_message_id)
AND COALESCE(oc.root_cid,oc.cid)=COALESCE(c.root_cid,c.cid)
AND (oc.created_at,oc.user_message_id,oc.cid) < (c.created_at,c.user_message_id,c.cid))"""
