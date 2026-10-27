"""Single and batch suggestion paths share the accepted sibling predicate."""

from doceditor import comments
from doceditor.comment_batch_actions import _attach_accepted_insertions


def test_batch_groups_only_accepted_insertions_by_artifact_and_anchor(monkeypatch):
    def no_query(*args, **kwargs):
        raise AssertionError("batch should reuse loaded rows")

    monkeypatch.setattr(comments, "accepted_insertions", no_query)
    pending = {"artifact_id": "doc", "anchor_text": "Heading", "insert_after": True}
    accepted = {**pending, "id": "one", "decision": "accepted"}
    rejected = {**pending, "id": "two", "decision": "rejected"}
    other = {**accepted, "id": "three", "anchor_text": "Other"}
    _attach_accepted_insertions([pending], [pending, accepted, rejected, other])
    assert pending["_accepted_insertions"] == [accepted]
