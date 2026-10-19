"""The doc_comment_update description carries the same resolve rule as the
feedback-node framing: a person's comment stays open for them to resolve."""

from __future__ import annotations


def test_description_leaves_a_persons_comment_open():
    from plans.feedback_nodes import USER_FRAMING
    from tools.documents import DocCommentUpdateTool

    description = DocCommentUpdateTool().description
    rule = "Leave a person's comment open for them to resolve, above all one you replied to"
    assert rule in description
    assert "resolve it yourself only when they asked you to" in description
    assert "resolve it yourself only when they asked you to" in USER_FRAMING
