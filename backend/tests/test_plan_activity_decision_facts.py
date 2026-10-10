"""Actual compact owner reads preserve the persisted decision grammar."""
from __future__ import annotations

import json

import pytest

from plans import decision_projection as owner
from tests.test_plan_activity_index import db, _task
import plans.service as service


def _body():
    return dict.fromkeys(owner._BODY_REQUIRED) | {"state": "raised", "options": []}


def _cases():
    body = _body()
    base = json.dumps({"attention_state": "raised", "plan_decision": body})
    cases = [("valid_null_fields", base)]
    for field in owner._BODY_REQUIRED:
        missing = body.copy()
        del missing[field]
        cases.append(("missing_" + field, json.dumps({"plan_decision": missing})))
        cases.append(("null_" + field, json.dumps({"plan_decision": body | {field: None}})))
    for options in [[], [{}], [1, True, None], {}, "[]", 1, False, None]:
        cases.append(("options_" + repr(options), json.dumps({"plan_decision": body | {"options": options}})))
    for state in [*owner.STATES, "unknown", None, 1, True, [], {}, "RAISED"]:
        cases.append(("state_" + repr(state), json.dumps({"plan_decision": body | {"state": state}})))
    for metadata in ["", None, "{broken", "[", "null", "[]", "true", "1", '"text"', "{}",
                     '{"plan_decision":null}', '{"plan_decision":[]}', '{"plan_decision":"text"}']:
        cases.append(("metadata_" + repr(metadata), metadata))
    valid = json.dumps(body)
    cases.extend([
        ("duplicate_state_invalid", '{"plan_decision":' + valid[:-1] + ',"state":"unknown"}}'),
        ("duplicate_state_valid", base.replace('"state": "raised"', '"state":"unknown","st\\u0061te":"raised"')),
        ("duplicate_body_empty", '{"plan_decision":' + valid + ',"plan_decision":{}}'),
        ("duplicate_body_valid", '{"plan_decision":{},"plan_decis\\u0069on":' + valid + '}'),
        ("duplicate_options_object", base.replace('"options": []', '"options": [],"options": {}')),
        ("duplicate_options_array", base.replace('"options": []', '"options": {},"options": []')),
        ("attention_cleared_last", base[:-1] + ',"attention_state":"cleared"}'),
        ("attention_raised_last", base.replace('"attention_state": "raised"', '"attention_state":"cleared","attention_st\\u0061te":"raised"')),
        ("scalar_options", base.replace('"options": []', '"options": [1, true, null]')),
        ("large_body", json.dumps({"plan_decision": body | {"context": "FULL_CONTEXT" * 16384, "options": [{"label": "FULL_OPTION" * 16384}]}})),
    ])
    for token in ["NaN", "Infinity", "-Infinity", "1e9999", '"\\ud800"', '"NaN Infinity -Infinity"',
                  "/*bad*/null", "'bad'", "0x10", "+1", ".5", "01", "NAN", "Inf", "+Infinity",
                  '"\\x41"', '"\\Infinity"', '"\\NaN"', '"unfinished']:
        cases.append(("context_" + token, base.replace('"context": null', '"context":' + token)))
    cases.extend([
        ("comment_prefix", "/*bad*/" + base),
        ("trailing_comma", base[:-1] + ",}"),
        ("unquoted_key", base.replace('"context": null', "context:null")),
        ("second_document", base + " {}"),
        ("nan_state", base.replace('"state": "raised"', '"state":NaN')),
        ("nonfinite_options", base.replace('"options": []', '"options": [NaN, Infinity, -Infinity]')),
    ])
    return cases


@pytest.mark.parametrize("name,metadata", _cases(), ids=lambda item: str(item)[:60])
def test_actual_activity_adapter_uses_exact_owner_grammar(db, name, metadata):
    plan = service.ensure_plan("thread", "moving", db=db)
    review = _task(plan.id, "Review", db=db)
    db.execute("UPDATE plan_nodes SET state='review_requested' WHERE id=?", (review.id,))
    db.execute("""INSERT INTO artifact_identities
        (id,integration,type,external_id,state,metadata,created_at,updated_at)
        VALUES ('artifact','local','plan-decision','decision','active',?,'t','t')""", (metadata,))
    db.execute("""INSERT INTO plan_node_artifact_links
        (node_id,artifact_id,role,provenance,created_at)
        VALUES (?,'artifact','attention','test','t')""", (review.id,))
    expected = owner._body({"metadata": metadata})
    facts = owner.activity_facts([plan.id], db=db)
    assert bool(facts) == (expected is not None), name
    if expected is not None:
        assert facts[0]["decision_state"] == expected["state"]
        assert facts[0]["source_node_id"] == expected["source_node_id"]
        assert facts[0]["attention_state"] == owner._attention_state({"metadata": metadata})
        assert not ({"metadata", "options", "context", "body"} & facts[0].keys())
        assert "FULL_CONTEXT" not in json.dumps(facts)
        assert "FULL_OPTION" not in json.dumps(facts)


@pytest.mark.parametrize("case", ["live", "missing_owner", "archived_owner", "purged_owner", "lifecycle_archived",
    "archived_plan", "review_not_requested", "source_closed", "source_archived", "source_absent",
    "source_other_plan", "source_retired_branch", "source_null", "review_closed", "attention_cleared",
    "draft", "parent_reviewed", "answered", "invalidated"])
def test_actual_activity_adapter_matches_queue_answerability(db, case):
    plan = service.ensure_plan("thread", "moving", db=db)
    source = _task(plan.id, "Source", db=db)
    review = _task(plan.id, "Review", db=db)
    db.execute("UPDATE plan_nodes SET state='review_requested' WHERE id=?", (review.id,))
    body = _body() | {"plan_id": plan.id, "node_id": review.id, "source_node_id": source.id,
                      "thread_id": "moving", "order_key": 0, "revision": 1}
    if case in owner.STATES:
        body["state"] = case
    if case == "source_absent":
        body["source_node_id"] = "absent"
    if case == "source_null":
        body["source_node_id"] = None
    if case == "source_other_plan":
        peer = service.ensure_plan("thread", "blocked", db=db)
        body["source_node_id"] = _task(peer.id, "Foreign source", db=db).id
    metadata = json.dumps({"attention_state": "cleared" if case == "attention_cleared" else "raised", "plan_decision": body})
    db.execute("""INSERT INTO artifact_identities
        (id,integration,type,external_id,state,metadata,created_at,updated_at)
        VALUES ('artifact','local','plan-decision','decision','active',?,'t','t')""", (metadata,))
    db.execute("""INSERT INTO plan_node_artifact_links
        (node_id,artifact_id,role,provenance,created_at)
        VALUES (?,'artifact','attention','test','t')""", (review.id,))
    if case == "missing_owner":
        # The fixture has a spawned child; clear that FK before exercising the
        # schema-permitted orphan plan owner (not the normal deletion writer).
        db.execute("UPDATE threads SET spawned_by=NULL WHERE spawned_by='moving'")
        db.execute("DELETE FROM threads WHERE id='moving'")
    if case in {"archived_owner", "purged_owner", "lifecycle_archived"}:
        field, value = {"archived_owner": ("archived_at", "t"), "purged_owner": ("purged_at", "t"),
                        "lifecycle_archived": ("lifecycle_state", "archived")}[case]
        db.execute(f"UPDATE threads SET {field}=? WHERE id='moving'", (value,))
    if case == "archived_plan":
        db.execute("UPDATE plans SET state='archived' WHERE id=?", (plan.id,))
    if case in {"source_closed", "source_archived"}:
        field, value = ("state", "closed") if case == "source_closed" else ("archived_at", "t")
        db.execute(f"UPDATE plan_nodes SET {field}=? WHERE id=?", (value, source.id))
    if case in {"review_closed", "review_not_requested"}:
        db.execute("UPDATE plan_nodes SET state=? WHERE id=?", ("closed" if case == "review_closed" else "active", review.id))
    if case == "source_retired_branch":
        parent = service.add_node(plan.id, kind="phase", title="Retired", acceptance_criteria="done", db=db)
        db.execute("UPDATE plan_nodes SET parent_id=?,state='accepted' WHERE id=?", (parent.id, source.id))
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (parent.id,))
    facts = owner.activity_facts([plan.id], db=db)
    assert {item["artifact_id"] for item in facts if item["answerable"]} == {
        item["artifact_id"] for item in owner.raised_decisions([plan.id], db=db)
    }
    assert any(item["answerable"] for item in facts) == (case == "live")
