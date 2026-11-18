"""Notice the text saved in updates and projected by plan decisions."""

import json

import core.attention_notice as notice
from plans import public
from tests.test_attention_notice import _Open
from tests.test_attention_notice_update_links import _post, workspace  # noqa: F401
from tests.test_plan_decisions import db, _delegated, _draft, _mid, _thread_actor  # noqa: F401

CHECK = (
    "Before the person reads it: does each line carry its whole idea without leaving them a "
    "question, and does it say whether it is how things are today, a plan, or already done? "
    "If not, rewrite it now."
)


def test_saved_update_and_replacement_use_saved_text(db, workspace, monkeypatch):
    events = []
    monkeypatch.setattr(notice, "get_db", lambda: _Open(db))
    monkeypatch.setattr(notice, "emit_product_event", lambda *a, **k: events.append(k))
    args, receipt = _post(db, "Three clear words.")
    args["replace"] = ["previous"]
    args["text"] = "This argument is not the saved text."
    rendered = notice.attention_notice(thread_id="owner", tool_name="post_update", arguments=args, result=receipt)
    assert rendered == (
        "[CORSIA attention notice] Your update: 3 words, at least 1 minute to read.\n"
        + CHECK + " (replace it while it is unseen)"
    )
    assert events[0]["properties"]["handover"] == "update"
    assert events[0]["properties"]["update_words"] == 3
    assert events[0]["properties"]["waiting_documents"] is None


def test_notice_failure_does_not_change_a_saved_call(monkeypatch, caplog):
    def fail_open():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(notice, "get_db", fail_open)
    with caplog.at_level("WARNING", logger=notice.__name__):
        rendered = notice.attention_notice(
            thread_id="owner", tool_name="post_update", arguments={"text": "Saved."},
            result="Update saved as message id-1.",
        )
    assert rendered is None
    assert "could not project post_update" in caplog.text


def test_decision_draft_and_update_are_read_from_public_projection(db, monkeypatch):
    events = []
    monkeypatch.setattr(notice, "get_db", lambda: _Open(db))
    monkeypatch.setattr(notice, "emit_product_event", lambda *a, **k: events.append(k))
    plan, source = _delegated(db)
    decision = _draft(db, plan, source)
    receipt = json.dumps({"decision": decision})
    draft = notice.attention_notice(thread_id="worker", tool_name="plan_decision", arguments={"operation": "draft"}, result=receipt)
    assert draft == (
        "[CORSIA attention notice] Your decision: 39 words, at least 1 minute to read.\n"
        + CHECK
    )
    assert events[-1]["properties"]["handover"] == "decision"
    assert events[-1]["properties"]["waiting_documents"] is None
    previous_words = events[-1]["properties"]["words"]
    changed = public.update_decision(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_thread_actor(), provenance="test", db=db, context="A much shorter context.",
    ).result["decision"]
    updated = notice.attention_notice(thread_id="worker", tool_name="plan_decision", arguments={"operation": "update"}, result=json.dumps({"decision": changed}))
    assert updated and updated.endswith(CHECK)
    assert events[-1]["properties"]["words"] < previous_words
    assert notice.attention_notice(thread_id="worker", tool_name="plan_decision", arguments={"operation": "update"}, result="Error: refused") is None
    assert len(events) == 2
