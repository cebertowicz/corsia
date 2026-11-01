"""The continuity_edit schema must describe what its validator enforces.

An agent produces a call from the tool definition alone. When the schema is
looser than the validator, that call is rejected at runtime and the shape has
to be discovered by trial — which is the failure #1477 records, and which this
lane hit twice in one session.

These tests are drift guards as much as assertions: they derive what the schema
must expose from the validator models themselves, so adding a field or changing
a body shape cannot silently leave the schema behind.
"""

from __future__ import annotations

import json

import pytest


_REF = {"kind": "runtime", "reference": "r"}


def _schema() -> dict:
    """The parameters block an agent actually receives."""
    from tools.continuity import ContinuityEditTool

    return ContinuityEditTool().parameters


def _body() -> dict:
    """The body schema an agent receives: one object, the union of the shapes."""
    return _schema()["properties"]["body"]


def _refuses(call: dict) -> str:
    """The refusal the real tool path gives a call, or fail if it is accepted.

    ``ToolRegistry.execute`` decodes and validates before any handler runs, so
    a refusal here is the one an agent receives.
    """
    import asyncio

    from tools.continuity import ContinuityEditTool
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ContinuityEditTool())
    result = asyncio.run(registry.execute("continuity_edit", call))
    assert result.startswith("Error: Invalid parameters for tool 'continuity_edit'"), result
    return result


def _decoded(call: dict) -> dict:
    """The call as the registry hands it to validation (Tool.decode_string_arguments)."""
    from tools.continuity import ContinuityEditTool

    return ContinuityEditTool().decode_string_arguments(call)


def test_a_json_string_body_is_decoded_once_on_add():
    """A double-encoded body is the majority recoverable rejection (#1653).

    The same threads recorded successful object-body adds minutes apart, so
    the model is not confused about the shape -- it serialised it twice. The
    registry decodes it for every tool; the body's oneOf of objects counts as
    an object.
    """
    import json

    from tools.continuity import ContinuityEditTool

    body = {"statement": "s", "source_refs": [_REF]}
    call = _decoded({"operation": "add", "field": "facts", "body": json.dumps(body)})

    assert call["body"] == body
    assert ContinuityEditTool().validate_params(call) == []


def test_a_json_string_body_is_decoded_once_on_replace():
    import json

    from tools.continuity import ContinuityEditTool

    body = {"statement": "s", "source_refs": [_REF]}
    call = _decoded({"operation": "replace", "entry_id": 1, "body": json.dumps(body)})

    assert call["body"] == body
    assert ContinuityEditTool().validate_params(call) == []


@pytest.mark.parametrize("text", ["not json", '["s"]'])
@pytest.mark.parametrize("call", [
    {"operation": "add", "field": "facts"},
    {"operation": "replace", "entry_id": 1},
])
def test_a_string_body_that_is_not_an_object_is_refused_once(text, call):
    """Not JSON, or JSON that is not an object: left as text, then refused once,
    with the message every tool gives for text where an object belongs."""
    from tools.continuity import ContinuityEditTool

    call = _decoded({**call, "body": text})

    assert call["body"] == text
    assert ContinuityEditTool().validate_params(call) == [
        "body should be object, not text: pass the object itself"
    ]


def test_body_exposes_every_key_the_validator_requires():
    """No required body key may be invisible in the schema.

    Derived from the validator's own models, so a new entry shape cannot ship
    with the schema still describing the old one.
    """
    from core.continuity_types import ENTRY_FIELD_MODELS

    required = {
        name
        for model in ENTRY_FIELD_MODELS.values()
        for name, info in model.model_fields.items()
        if info.is_required()
    }
    exposed = set(_body()["properties"])

    assert required, "validator exposed no required fields; test is not measuring"
    assert required <= exposed, f"hidden from the agent: {sorted(required - exposed)}"


@pytest.mark.parametrize("operation", ["add", "replace"])
@pytest.mark.parametrize(
    "body",
    [
        {"statement": "x" * 4097, "source_refs": [_REF]},
        {"statement": "", "source_refs": [_REF]},
        {"statement": "s", "source_refs": [_REF] * 17},
    ],
    ids=["too-long", "empty", "too-many-refs"],
)
def test_bounds_left_out_of_the_schema_are_enforced_by_the_server(operation, body):
    """The advertised body omits length and item bounds the entry models hold.

    They are enforced where every body is validated (``validate_entry_body``
    through ``body_errors``), so leaving them out of the prompt loses nothing.
    """
    call = {"operation": operation, "body": body}
    call |= {"field": "facts"} if operation == "add" else {"entry_id": 1}

    refusal = _refuses(call)

    assert "invalid facts entry" in refusal or "does not match any continuity entry shape" in refusal


def test_the_body_schema_does_not_restate_bounds_the_server_enforces():
    """Guards the token saving: a bound back in the body costs every prompt."""
    text = json.dumps(_body())

    for bound in ("minLength", "maxLength", "maxItems"):
        assert bound not in text


def test_relevant_state_kind_is_the_restricted_vocabulary():
    """`kind` is a closed set in the validator, not free text.

    Distinct from a source reference's `kind`, which has its own wider
    vocabulary — so this asserts the state branch specifically.
    """
    from core.continuity_types import RelevantState
    from typing import get_args

    expected = get_args(RelevantState.model_fields["kind"].annotation)
    assert set(_body()["properties"]["kind"]["enum"]) == set(expected)


def test_unknown_body_keys_are_refused_by_the_schema():
    """The validator forbids extras; the schema has to say so.

    Without this a body with a stray key passes the advertised schema and is
    rejected at runtime — the whole class of defect this change removes.
    """
    body = _body()
    assert body["additionalProperties"] is False
    assert body["properties"]["source_refs"]["items"]["additionalProperties"] is False


def test_source_refs_shape_is_declared_not_prose():
    """`source_refs` is a list of objects, and the schema has to say so.

    The reported repro is an agent supplying a list of strings, which the bare
    object schema permitted and the validator rejected.
    """
    from core.continuity_types import SourceKind
    from typing import get_args

    refs = _body()["properties"]["source_refs"]

    assert refs["type"] == "array"
    items = refs["items"]
    assert items["type"] == "object"
    assert set(items["required"]) == {"kind", "reference"}
    assert set(items["properties"]["kind"]["enum"]) == set(get_args(SourceKind))


@pytest.mark.parametrize(
    "field,body,valid",
    [
        ("facts", {"statement": "s", "source_refs": [_REF]}, True),

        ("decisions", {"statement": "s", "rationale": "w", "source_refs": [_REF]}, True),
        (
            "relevant_state",
            {"kind": "file", "reference": "r", "state": "s", "source_refs": [_REF]},
            True,
        ),
        ("verification", {"result": "r", "evidence": "e", "source_refs": [_REF]}, True),
        # The reported repro: source refs as bare strings.
        ("facts", {"statement": "s", "source_refs": ["thread 0fd264e6"]}, False),
        ("facts", {"statement": "s", "source_refs": [_REF], "stray": 1}, False),
        ("facts", {"statement": "s", "source_refs": [_REF], "rationale": "w"}, False),
        (
            "relevant_state",
            {"kind": "nonsense", "reference": "r", "state": "s", "source_refs": [_REF]},
            False,
        ),
    ],
)
def test_schema_accepts_exactly_what_the_validator_accepts(field, body, valid):
    """The property that actually matters, checked end to end.

    Every other test here inspects the schema's internals. This runs a real
    JSON Schema validator and the real Pydantic validator over the same call
    and requires them to agree, because the defect is precisely a body that
    passes one and fails the other.

    The whole call is validated, not the body alone: the shapes are bound to
    fields, so a body checked without its field is not the thing an agent
    sends.
    """
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError

    from core.continuity_types import ENTRY_FIELD_MODELS

    call = {"operation": "add", "field": field, "body": body}
    schema_ok = Draft202012Validator(_schema()).is_valid(call)
    try:
        ENTRY_FIELD_MODELS[field].model_validate(body)
        validator_ok = True
    except ValidationError:
        validator_ok = False

    assert schema_ok == validator_ok == valid, (
        f"schema says {schema_ok}, validator says {validator_ok}"
    )


def test_every_validator_shape_has_exactly_one_generated_rule():
    """One per-field rule per distinct entry shape, and the body is their union.

    A shape declared twice would be two places to update; a shape missing
    means some field accepts anything.
    """
    from core.continuity_types import ENTRY_FIELD_MODELS
    from tools.continuity_schema import field_body_rules

    shapes = {
        tuple(sorted(model.model_fields))
        for model in ENTRY_FIELD_MODELS.values()
    }
    generated = [
        tuple(sorted(rule["then"]["properties"]["body"]["properties"]))
        for rule in field_body_rules()
    ]

    assert sorted(generated) == sorted(shapes)
    assert len(generated) == len(set(generated)), "duplicate generated shapes"
    assert set(_body()["properties"]) == {key for shape in shapes for key in shape}


def _shape_for(model) -> dict:
    """One minimal body the given validator model accepts."""
    return {
        "_Statement": {"statement": "s", "source_refs": [_REF]},
        "_Decision": {"statement": "s", "rationale": "w", "source_refs": [_REF]},
        "_RelevantState": {
            "kind": "file", "reference": "r", "state": "s", "source_refs": [_REF],
        },
        "_Outcome": {"result": "r", "evidence": "e", "source_refs": [_REF]},
    }[model.__name__]


def _every_field_and_shape():
    """Every (field, body-shape) pair, so no combination goes unchecked."""
    from core.continuity_types import ENTRY_FIELD_MODELS

    models = list(dict.fromkeys(ENTRY_FIELD_MODELS.values()))
    return [
        (field, model, ENTRY_FIELD_MODELS[field] is model)
        for field in ENTRY_FIELD_MODELS
        for model in models
    ]


@pytest.mark.parametrize("field,model,should_accept", _every_field_and_shape())
def test_the_whole_call_admits_only_the_shape_that_field_accepts(
    field, model, should_accept,
):
    """The property the whole change is for, over every field-bearing call.

    When the field is known the body's shape is fully determined, so all
    three layers must agree exactly: the advertised JSON Schema, CORSIA's
    own tool validator, and the Pydantic model. Every field crossed with
    every shape. Validating a body without its field was how earlier
    revisions kept missing this.
    """
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError

    from core.continuity_types import ENTRY_FIELD_MODELS
    from tools.continuity import ContinuityEditTool

    body = _shape_for(model)
    call = {"operation": "add", "field": field, "body": body}

    schema_ok = Draft202012Validator(_schema()).is_valid(call)
    tool_ok = not ContinuityEditTool().validate_params(call)
    try:
        ENTRY_FIELD_MODELS[field].model_validate(body)
        runtime_ok = True
    except ValidationError:
        runtime_ok = False

    assert schema_ok == tool_ok == runtime_ok == should_accept, (
        f"add field={field} with a {model.__name__} body: "
        f"schema={schema_ok}, tool={tool_ok}, runtime={runtime_ok}, "
        f"expected={should_accept}"
    )


def test_replace_keeps_every_shape_available_without_a_field():
    """replace names an entry by id, so the schema cannot know its field.

    The field-dependent rules must therefore not constrain it: pinning one
    shape here would reject valid edits to every entry of another shape.
    """
    from jsonschema import Draft202012Validator

    from core.continuity_types import ENTRY_FIELD_MODELS

    validator = Draft202012Validator(_schema())
    for model in dict.fromkeys(ENTRY_FIELD_MODELS.values()):
        call = {"operation": "replace", "entry_id": 1, "body": _shape_for(model)}
        assert validator.is_valid(call), (
            f"replace rejected a valid {model.__name__} body"
        )


def test_every_field_is_covered_by_a_body_rule():
    """A new field with a new shape must not slip through unconstrained.

    Without this, adding a field whose model is not in the rules would leave
    it accepting any shape — the original defect, reintroduced quietly.
    """
    from core.continuity_types import ENTRY_FIELD_MODELS
    from tools.continuity_schema import field_body_rules

    covered = {
        field
        for rule in field_body_rules()
        for field in rule["if"]["properties"]["field"]["enum"]
    }

    assert covered == set(ENTRY_FIELD_MODELS)


def test_the_platform_validator_rejects_a_replace_body_of_junk():
    """A junk replace body is refused, and now by the schema as well.

    This was the reviewer's original case. It used to pass the advertised
    schema and fail only at runtime, because replace named no field for a
    rule to key on.
    """
    from tools.continuity import ContinuityEditTool

    errors = ContinuityEditTool().validate_params(
        {"operation": "replace", "field": "facts", "entry_id": 1,
         "body": {"junk": 1}}
    )

    assert errors, "a body matching no entry shape was accepted"


def test_the_platform_validator_rejects_a_wrong_shape_for_the_named_field():
    """`add` knows its field, so the exact model applies, not merely some model."""
    from tools.continuity import ContinuityEditTool

    decision_body = {
        "statement": "s", "rationale": "r", "source_refs": [_REF],
    }
    errors = ContinuityEditTool().validate_params(
        {"operation": "add", "field": "facts", "body": decision_body}
    )

    assert errors, "a decision body was accepted for a statement field"


@pytest.mark.parametrize("model_name", ["_Statement", "_Decision", "_RelevantState", "_Outcome"])
def test_replace_accepts_every_real_shape(model_name):
    """replace takes no field, so every legal shape must stay writable.

    Its entry's field is a database fact this layer cannot read, and asking
    the agent to repeat it would add a remembered input for nothing (PS24).
    So the schema narrows replace to "one of the four" and no further.
    """
    from core.continuity_types import ENTRY_FIELD_MODELS
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    model = next(
        m for m in dict.fromkeys(ENTRY_FIELD_MODELS.values())
        if m.__name__ == model_name
    )
    call = {"operation": "replace", "entry_id": 1, "body": _shape_for(model)}

    assert Draft202012Validator(_schema()).is_valid(call), (
        f"schema rejected a valid {model_name} replace body"
    )
    assert ContinuityEditTool().validate_params(call) == []


def test_replace_refuses_a_body_of_no_known_shape_in_both_layers():
    """`{"junk": 1}` is no entry shape: refused in the schema and by the server.

    The body schema is one object of every legal key, so a stray key fails it;
    a body of legal keys that make no shape (below) is refused by `body_errors`.
    """
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    call = {"operation": "replace", "entry_id": 1, "body": {"junk": 1}}

    assert not Draft202012Validator(_schema()).is_valid(call)
    assert ContinuityEditTool().validate_params(call)


def test_add_refuses_a_body_of_the_wrong_shape_for_its_field():
    """The refusal lives in the server, through the real tool path.

    A decision body sent to a statement field names the field it broke.
    """
    refusal = _refuses({
        "operation": "add", "field": "facts",
        "body": {"statement": "s", "rationale": "w", "source_refs": [_REF]},
    })

    assert "facts" in refusal
    assert "rationale" in refusal


@pytest.mark.parametrize("body", [
    {"statement": "s", "result": "r", "source_refs": [_REF]},
    {"statement": "s", "evidence": "e"},
    {"statement": "s", "rationale": "w"},
])
def test_replace_refuses_legal_keys_that_make_no_shape(body):
    """Every key is advertised, so a mix of shapes passes the body schema.

    Replace names no field; the server still refuses a body that is no entry.
    """
    refusal = _refuses({"operation": "replace", "entry_id": 1, "body": body})

    assert "does not match any continuity entry shape" in refusal


def test_replace_refuses_a_valid_shape_that_is_not_the_entrys_shape():
    """A decision body is a legal entry, but not for a facts entry: the store refuses.

    The entry's field is a database fact the tool layer cannot see, so the
    store validates against the stored field.
    """
    import sqlite3

    from infra.continuity_schema import CONTINUITY_DDL
    from infra.continuity_store import ContinuityError, add_entry, replace_entry

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO threads (id) VALUES ('t1')")
    for statement in CONTINUITY_DDL:
        conn.execute(statement)
    entry_id = add_entry(
        conn, thread_id="t1", history_epoch=1, field="facts",
        body={"statement": "s", "source_refs": []},
    )

    with pytest.raises(ContinuityError, match="invalid facts entry"):
        replace_entry(
            conn, thread_id="t1", history_epoch=1, entry_id=entry_id,
            body={"statement": "s", "rationale": "w", "source_refs": []},
        )


def test_replace_accepts_a_redundant_matching_field_parameter():
    """A caller-supplied `field` on replace is now legal (#1653).

    About 76% of production rejections were this exact call. Whether it
    *matches* the entry's stored field is a database fact this schema layer
    cannot read; that enforcement is in the store, covered by
    test_continuity_store.py and test_continuity_staging.py.
    """
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    call = {"operation": "replace", "field": "facts", "entry_id": 1,
            "body": _shape_for(_model_named("_Statement"))}

    assert Draft202012Validator(_schema()).is_valid(call)
    assert ContinuityEditTool().validate_params(call) == []


def test_drop_accepts_a_redundant_field_parameter():
    """Same relief for drop, the second largest rejection class (#1653)."""
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    call = {"operation": "drop", "field": "facts", "entry_id": 1,
            "reason": "superseded"}

    assert Draft202012Validator(_schema()).is_valid(call)
    assert ContinuityEditTool().validate_params(call) == []


def test_inspect_accepts_a_field_parameter_with_or_without_entry_id():
    """`field` filters a listing, or confirms one indexed entry (#1653)."""
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    listing = {"operation": "inspect", "field": "facts"}
    indexed = {"operation": "inspect", "field": "facts", "entry_id": 1}

    for call in (listing, indexed):
        assert Draft202012Validator(_schema()).is_valid(call), call
        assert ContinuityEditTool().validate_params(call) == [], call


def _model_named(name: str):
    from core.continuity_types import ENTRY_FIELD_MODELS

    return next(
        m for m in dict.fromkeys(ENTRY_FIELD_MODELS.values())
        if m.__name__ == name
    )


def _clause_for(operation: str) -> dict:
    """The one generated rule that governs a given operation."""
    from tools.continuity_schema import operation_rules

    clauses = [
        rule for rule in operation_rules()
        if rule["if"]["properties"]["operation"]["const"] == operation
    ]
    assert len(clauses) == 1, f"{operation} has {len(clauses)} rules, want 1"
    return clauses[0]


@pytest.mark.parametrize("operation", ["inspect", "add", "replace", "drop"])
def test_each_operation_constrains_its_parameters_in_the_schema(operation):
    """The per-operation contract must be constraints, not description text.

    Prose in the `operation` description is a hint a reader may follow. What
    a schema consumer checks against is the constraint, so `{"operation":
    "add"}` and a `replace` carrying `field` were both advertised as legal
    and rejected at runtime.

    Derived from the maps the executor enforces, so the clause cannot drift
    from them.
    """
    from tools.continuity_schema import _OPERATION_FIELDS, _REQUIRED_FIELDS

    then = _clause_for(operation)["then"]

    assert then["type"] == "object"
    assert set(then["properties"]) == {"operation", *_OPERATION_FIELDS[operation]}
    assert set(then["required"]) == {"operation", *_REQUIRED_FIELDS[operation]}
    assert then["additionalProperties"] is False


def test_an_operation_missing_a_required_parameter_is_refused_by_both_layers():
    """`{"operation": "add"}` passed the advertised schema and failed at runtime."""
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    call = {"operation": "add"}

    assert not Draft202012Validator(_schema()).is_valid(call)
    errors = ContinuityEditTool().validate_params(call)
    assert any("field" in error for error in errors), errors
    assert any("body" in error for error in errors), errors


def test_inspect_accepts_an_optional_target_entry():
    """The cold writer can fetch one indexed entry without loading the store."""
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    call = {"operation": "inspect", "entry_id": 3}

    assert Draft202012Validator(_schema()).is_valid(call)
    assert ContinuityEditTool().validate_params(call) == []


@pytest.mark.parametrize(
    "call",
    [
        {"operation": "inspect"},
        {"operation": "inspect", "entry_id": 2},
        {"operation": "drop", "entry_id": 2, "reason": "superseded"},
        {"operation": "drop", "entry_id": 2, "reason": "superseded",
         "source_ref": "message 12"},
    ],
)
def test_the_operation_rules_still_admit_every_legal_call(call):
    """A closed contract must not close over a call the executor accepts."""
    from jsonschema import Draft202012Validator
    from tools.continuity import ContinuityEditTool

    assert Draft202012Validator(_schema()).is_valid(call), call
    assert ContinuityEditTool().validate_params(call) == []


def test_field_parameter_says_which_operation_it_belongs_to():
    """The exact confusion in the report: `field` looks universally optional."""
    text = _schema()["properties"]["field"]["description"]
    assert "add" in text


@pytest.mark.parametrize(
    "field,expected",
    [
        ("facts", {"statement", "source_refs"}),
        ("decisions", {"statement", "rationale", "source_refs"}),
        ("relevant_state", {"kind", "reference", "state", "source_refs"}),
        ("verification", {"result", "evidence", "source_refs"}),
    ],
)
def test_body_description_maps_each_field_group_to_its_keys(field, expected):
    """An agent must be able to read which keys this field needs.

    One representative field per validator model. The mapping is what turns a
    schema-valid call into a validator-valid one.
    """
    description = _body()["description"]

    assert field in description
    for key in expected:
        assert key in description
