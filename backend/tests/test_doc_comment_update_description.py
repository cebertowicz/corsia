"""Both places an agent learns who resolves a person's comment carry the one rule:
the feedback-node framing and the doc_comment_update description."""

from __future__ import annotations


def test_both_surfaces_carry_the_whole_resolve_rule():
    from doceditor.comment_state_updates import PERSON_COMMENT_RESOLVE_RULE
    from plans.feedback_nodes import USER_FRAMING
    from tools.documents import DocCommentUpdateTool

    assert PERSON_COMMENT_RESOLVE_RULE in DocCommentUpdateTool().description
    assert PERSON_COMMENT_RESOLVE_RULE in USER_FRAMING
