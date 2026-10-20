"""doc_comment_update's description carries the one rule for who resolves a
person's comment, read from its single constant."""

from __future__ import annotations


def test_description_carries_the_whole_resolve_rule():
    from doceditor.comment_resolve_rule import PERSON_COMMENT_RESOLVE_RULE
    from tools.documents import DocCommentUpdateTool

    assert PERSON_COMMENT_RESOLVE_RULE in DocCommentUpdateTool().description
