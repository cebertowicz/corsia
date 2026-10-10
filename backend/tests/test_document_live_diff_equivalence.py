"""Faster forced line alignment must keep exactly the prior CRDT operations."""
from difflib import SequenceMatcher
import random

import pytest

from doceditor import live_diff
from doceditor.live_text import _apply_changes, create
from tests.test_document_live_diff_cost import prose_rewrite


@pytest.mark.asyncio
async def test_line_alignment_keeps_prior_operations_and_text(monkeypatch):
    rng = random.Random(74123)
    corpus = [("", "é🙃\r\n"), ("é🙃\r\n", ""), ("hello world", "hello there"),
              ("a😀Z", "aé🙃Z"), ("one  two", "one two"),
              ("a\r\nb\r\nc\r\n", "c\r\na\r\nb\r\n"),
              ("repeated\n" * 300, "repeated\n" * 299 + "last\n"), prose_rewrite()]
    words, endings = ['plain', 'é', '🙃', '重复'], ['\n', '\r\n']
    for _ in range(400):
        rows = [f"{index} {rng.choice(words)}{rng.choice(endings)}"
                for index in range(rng.randrange(1, 35))]
        before = "".join(rows)
        kind = rng.randrange(5)
        if kind == 0:
            rows.insert(rng.randrange(len(rows) + 1), "new 😀\r\n")
        elif kind == 1:
            del rows[rng.randrange(len(rows))]
        elif kind == 2:
            moved = rows.pop(rng.randrange(len(rows)))
            rows.insert(rng.randrange(len(rows) + 1), moved)
        elif kind == 3:
            index = rng.randrange(len(rows))
            rows[index] = rows[index].replace(" ", " revised ", 1)
        else:
            rows.insert(rng.randrange(len(rows) + 1), rng.choice(rows))
        corpus.append((before, "".join(rows)))
    for before, after in corpus:
        actual = live_diff.text_changes(before, after)
        with monkeypatch.context() as old:
            old.setattr(live_diff, "_line_opcodes", lambda a, b: SequenceMatcher(a=a, b=b).get_opcodes())
            expected = live_diff.text_changes(before, after)
        assert actual == expected, (before[:200], after[:200])
        assert await live_diff.text_changes_async(before, after) == expected
        doc = create(before)
        _apply_changes(doc, actual)
        assert str(doc["text"]) == after
