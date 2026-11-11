"""The answer-tag grammar has one home, and its prefix check cannot drift from it."""

import ast
import random
import re
from pathlib import Path

from infra.chat_comment_ids import CID_PATTERN, could_start_cid, format_cid

_GRAMMAR_HOMES = {'infra/chat_comment_ids.py', 'infra/chat_comment_answers.py'}


def test_private_answer_grammar_is_not_imported_elsewhere():
    backend = Path(__file__).resolve().parents[1]
    private = {'_TAGGED', '_TAG', '_CID'}
    violations = []
    for path in backend.rglob('*.py'):
        if str(path.relative_to(backend)) in _GRAMMAR_HOMES:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in ('infra.chat_comment_answers', 'infra.chat_comment_ids'):
                if private.intersection(alias.name for alias in node.names):
                    violations.append(str(path.relative_to(backend)))
    assert not violations


def test_every_start_of_a_cid_passes_the_prefix_check():
    rng = random.Random(2433)
    cids = [format_cid(rng.randint(1, 10 ** rng.randint(1, 6)), rng.randint(1, 10 ** rng.randint(1, 4)))
            for _ in range(500)] + [format_cid(1, 1), format_cid(10, 10), format_cid(109, 2005)]
    for cid in cids:
        assert re.fullmatch(CID_PATTERN, cid), cid
        for end in range(len(cid) + 1):
            assert could_start_cid(cid[:end]), cid[:end]


def test_text_that_cannot_become_a_cid_fails_the_prefix_check():
    for text in ('x', 'c0', 'c1..', 'c1.0', 'c01', 'c1.1]', 'c1.1 ', 'c1 is', 'code', 'C1.1'):
        assert not could_start_cid(text), text


def test_stream_sends_the_same_chat_text_however_the_reply_is_chunked():
    from infra.chat_comment_answers import VisibleAnswerStream

    cases = {
        'Intro\n\n[c1.1] secret\n\n[c12.3] more': 'Intro',
        '[c1.1] secret': '',
        'Price [c1.1] is inline\n\nDone': 'Price [c1.1] is inline\n\nDone',
        'List\n\n[c1] not a tag\n\n[c2.1] secret': 'List\n\n[c1] not a tag',
        'Ends mid tag\n\n[c1.1': 'Ends mid tag',
    }
    for text, shown in cases.items():
        for first in range(len(text) + 1):
            for second in range(first, len(text) + 1):
                stream = VisibleAnswerStream()
                sent = ''.join(stream.feed(chunk) for chunk in (text[:first], text[first:second], text[second:]))
                assert sent == shown, (text, first, second, sent)
