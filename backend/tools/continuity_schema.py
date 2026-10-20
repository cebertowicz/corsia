"""The continuity_edit parameter schema, generated from its validators.

The schema and the validator disagreeing is the defect #1477 records: a call
an agent built from the tool definition alone was rejected at runtime, and the
real shape had to be found by trial. So nothing here is written by hand -- the
branches, the per-field rules and the prose are all derived from
``ENTRY_FIELD_MODELS`` and the maps the executor enforces.
"""

from __future__ import annotations

from typing import Any

from core.continuity_types import ENTRY_FIELD_MODELS, ENTRY_FIELDS
from core.placement_decide import ANSWER_SCHEMA
from core.placement_room import OPERATION_SCHEMA
from infra.continuity_store import ContinuityError, validate_entry_body
from infra.placement_marks import PLACE_VERDICTS, REVIEW_VERDICTS


_OPERATIONS = ("inspect", "add", "replace", "drop", "review", "place", "triage", "outcome")

#: Which verdicts each placement operation takes.
VERDICTS = {"review": REVIEW_VERDICTS, "place": PLACE_VERDICTS}

#: One field per distinct body model, for checking a body whose field is not
#: known -- the ``replace`` case. Going through a field rather than the models
#: directly keeps :func:`validate_entry_body` the only definition in play.
_ONE_FIELD_PER_MODEL = tuple(
    {model: field for field, model in reversed(ENTRY_FIELD_MODELS.items())}.values()
)


def _body_key_groups() -> str:
    """Describe which body keys each continuity field needs.

    Derived from the validator's own models rather than restated, because the
    schema being looser than the validator is the defect this repairs: a
    schema-valid call was rejected at runtime and the shape had to be found by
    trial (#1477).
    """
    groups: dict[tuple[str, ...], list[str]] = {}
    for field, model in ENTRY_FIELD_MODELS.items():
        keys = tuple(sorted(model.model_fields))
        groups.setdefault(keys, []).append(field)
    return "; ".join(
        f"{', '.join(sorted(fields))} "
        f"{'needs' if len(fields) == 1 else 'need'} "
        f"{{{', '.join(keys)}}}"
        for keys, fields in groups.items()
    )


def _inline_refs(schema: Any, defs: dict[str, Any]) -> Any:
    """Replace ``$ref`` pointers with the definitions they name.

    Tool schemas travel to several providers, and not all of them resolve
    ``$ref`` against a document root that a nested parameter does not have.
    Inlining keeps the shape self-describing wherever it is read.
    """
    if isinstance(schema, dict):
        target = schema.get("$ref")
        if isinstance(target, str) and target.startswith("#/$defs/"):
            return _inline_refs(defs[target.rsplit("/", 1)[1]], defs)
        return {
            key: _inline_refs(value, defs)
            for key, value in schema.items()
            if key not in ("title", "$defs")
        }
    if isinstance(schema, list):
        return [_inline_refs(item, defs) for item in schema]
    return schema


def _body_schema() -> dict[str, Any]:
    """Declare the four legal entry shapes, once, with their real constraints.

    Generated from the validator models, so the text bound, the restricted
    ``relevant_state.kind`` vocabulary and ``extra="forbid"`` all carry across
    rather than being restated (#1477 is a restatement drifting from what it
    restated).

    ``oneOf`` here does the work that does not depend on which field an entry
    belongs to: a body matching none of the four is refused for every
    operation, including ``replace``, which names its entry by id and cannot
    say which field that is. :func:`field_body_rules` then narrows to the one
    shape when the field IS known.
    """
    return {
        "oneOf": [_model_schema(model) for model in _distinct_models()],
        "description": (
            "The continuity entry body. Its required shape is set by the "
            "field: " + _body_key_groups() + ". source_refs is a list of "
            "objects with kind and reference, never a list of strings. For "
            "replace, use the shape inspect returned for that entry."
        ),
    }


def _distinct_models() -> list[Any]:
    """The body models, once each, in declaration order."""
    return list(dict.fromkeys(ENTRY_FIELD_MODELS.values()))


def _model_schema(model: Any) -> dict[str, Any]:
    schema = model.model_json_schema()
    return _inline_refs(schema, schema.get("$defs", {}))


def field_body_rules() -> list[dict[str, Any]]:
    """Narrow the body to the one shape a named field accepts.

    Deliberately NOT a second copy of the model schemas. Provider-side JSON
    Schema validators apply the types, bounds and vocabularies in
    :func:`_body_schema`; CORSIA applies those same rules by calling the store
    validator in :func:`body_errors`, because :class:`tools.base.Tool` does not
    implement ``oneOf``. All this clause must add is WHICH shape a named field
    takes. The key set alone discriminates the four mutually exclusive shapes,
    so it carries bare key names with empty schemas -- enough to select,
    nothing restated.

    That is what makes the field-precise guarantee affordable. Repeating the
    full shapes here cost about 740 tokens in every agent's prompt for
    information ``oneOf`` already carried.

    One clause per distinct model rather than per field, because thirteen
    fields share four shapes. Each applies only when ``field`` is present, so
    ``replace`` and ``drop`` keep the ``oneOf`` guarantee and no more --
    which is all any schema can offer them, since the entry's field is a
    database fact this layer cannot read.
    """
    rules = []
    for model in _distinct_models():
        fields = sorted(
            field
            for field, bound in ENTRY_FIELD_MODELS.items()
            if bound is model
        )
        shape = _model_schema(model)
        rules.append(_clause(
            {"field": {"enum": fields}},
            require=["field"],
            then={
                "properties": {
                    "body": {
                        "type": "object",
                        "required": sorted(shape.get("required", ())),
                        "properties": dict.fromkeys(sorted(shape["properties"]), {}),
                        "additionalProperties": False,
                    },
                },
            },
        ))
    return rules


def _clause(
    selector: dict[str, Any],
    *,
    require: list[str],
    then: dict[str, Any],
) -> dict[str, Any]:
    """Wrap one conditional rule so both schema validators can evaluate it.

    Same shape as :mod:`tools.plan_schemas`, with one addition: the condition
    requires the parameter it keys on. Without that, a call omitting that
    parameter satisfies every condition vacuously and collects every
    consequence.

    ``type: "object"`` is declared on both halves because
    :meth:`tools.base.Tool._validate` reads ``required``, ``properties`` and
    ``additionalProperties`` only for a schema that says it describes an
    object. A clause our own validator cannot evaluate is a description, not
    a contract -- and a description standing in for a contract is the defect
    this module exists to remove.
    """
    return {
        "if": {
            "type": "object",
            "required": require,
            "properties": selector,
        },
        "then": {"type": "object", **then},
    }


_OPERATION_FIELDS = {
    "triage": {"subject_key", "tier", "cause", "new_work", "new_work_signal", "target_file", "serious_lowered_reason"},
    "outcome": {"subject_key", "outcome", "reason", "lesson_id", "workflow_version"},
    "inspect": {"entry_id", "field"},
    "add": {"field", "body"},
    "replace": {"entry_id", "body", "field"},
    "drop": {"entry_id", "reason", "source_ref", "field"},
    "review": {
        "entry_id", "verdict", "destination", "quote", "placement", "room", "reason",
    },
    "place": {"entry_id", "verdict", "destination", "quote", "placement"},
}
_REQUIRED_FIELDS = {
    "triage": ("subject_key", "tier", "cause", "new_work", "target_file"),
    "outcome": ("subject_key", "outcome"),
    "inspect": (),
    "add": ("field", "body"),
    "replace": ("entry_id", "body"),
    "drop": ("entry_id", "reason"),
    "review": ("entry_id", "verdict"),
    "place": ("entry_id", "verdict"),
}


def operation_rules() -> list[dict[str, Any]]:
    """Constrain each operation to its own parameters, as schema.

    Generated from the maps the executor enforces, so the two cannot drift.
    These were prose in the ``operation`` description, which is a hint and not
    a contract: ``{"operation": "add"}`` and a ``replace`` carrying ``field``
    both satisfied a schema whose only global requirement was ``operation``,
    and were then rejected at runtime -- the schema-valid/runtime-invalid
    split #1477 records, one level up from the body shapes.

    Each clause closes the call to the parameters its operation accepts, so a
    foreign one is refused where the schema is read rather than only where the
    call is executed.
    """
    return [
        _clause(
            {"operation": {"const": operation}},
            require=["operation"],
            then={
                "properties": dict.fromkeys(
                    sorted({"operation", *_OPERATION_FIELDS[operation]}), {}
                ),
                "required": sorted({"operation", *_REQUIRED_FIELDS[operation]}),
                "additionalProperties": False,
            },
        )
        for operation in _OPERATIONS
    ]


def _field_description() -> str:
    """Describe which operations accept `field`, derived from the operation maps.

    Kept generated rather than typed twice: #1477 was the schema saying
    something the maps did not enforce, and the redundant-but-correct
    `field` on replace/drop/inspect that #1653 fixes was the same defect in
    the other direction -- a legal call the schema advertised as refused.
    """
    optional = sorted(
        operation
        for operation in _OPERATIONS
        if operation != "add" and "field" in _OPERATION_FIELDS[operation]
    )
    return (
        "The continuity field for the entry. Required on add, selecting "
        "the new entry's field. Optional on " + ", ".join(optional) + ": "
        "on inspect without entry_id it filters the listing to that field; "
        "on inspect with entry_id, and on replace and drop, it is a "
        "redundant but correct confirmation that must match the entry's "
        "stored field, or the call is refused naming the mismatch."
    )


_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "operation": {
        "type": "string",
        "enum": list(_OPERATIONS),
        "description": (
            "triage records one immutable correction triage fact; outcome records its one "
            "terminal outcome. inspect lists current entries and ids; add records a new entry; "
            "replace rewrites one by id; drop retires one by id. review is "
            "only for a checkpoint's placement review; place records that you "
            "placed a lesson yourself: written or already retires the entry "
            "once destination is read and contains quote; proposed queues "
            "placement for CORSIA to file as a proposal; trial queues it as a "
            "trial skill with a review date."
        ),
    },
    "subject_key": {
        "type": "string", "minLength": 1,
        "description": "The immutable correction subject named by the correction note.",
    },
    "tier": {
        "type": "string", "enum": ["not_correction", "instruction", "note", "miss", "serious"],
        "description": "The System correction tier.",
    },
    "cause": {
        "type": "string", "enum": ["wrong_rule", "no_foundation", "rule_gap", "process_gap", "not_delivered", "not_followed", "cant_be_done"],
        "description": "The first matching System correction cause.",
    },
    "new_work": {"type": "string", "enum": ["yes", "no"]},
    "new_work_signal": {
        "type": "string", "enum": ["task_kind", "agent", "workflow_version"],
        "description": "Required when new_work is yes; omitted otherwise.",
    },
    "target_file": {
        "type": "string", "minLength": 1,
        "description": "triage: a file loaded in this turn; trial admission: the changed file or new.",
    },
    "serious_lowered_reason": {
        "type": "string", "minLength": 1,
        "description": "Why an automatically proposed Serious tier was lowered, if applicable.",
    },
    "outcome": {
        "type": "string", "enum": ["fixed", "instruction", "watch", "lesson", "workflow", "dropped"],
    },
    "lesson_id": {"type": "string", "minLength": 1},
    "workflow_version": {"type": "string", "minLength": 1},
    "field": {
        "type": "string",
        "enum": list(ENTRY_FIELDS),
        "description": _field_description(),
    },
    "body": _body_schema(),
    "entry_id": {
        "type": "integer",
        "minimum": 1,
        "description": (
            "The entry to inspect, replace, or drop, as listed in the frozen "
            "continuity index or returned by inspect."
        ),
    },
    "reason": {
        "type": "string",
        "minLength": 1,
        "description": (
            "drop: why the entry stopped applying. review reaffirm: why it "
            "still belongs in continuity."
        ),
    },
    "source_ref": {
        "type": "string",
        "minLength": 1,
        "description": "The message or decision that superseded the entry.",
    },
    "verdict": {
        "type": "string",
        "enum": sorted({v for verdicts in VERDICTS.values() for v in verdicts}),
        "description": (
            "review: reaffirm (keep it, with reason), select or already. "
            "place: written, already, proposed or trial."
        ),
    },
    "destination": {
        "type": "string",
        "minLength": 1,
        "description": (
            "place written or already, review already: the file where the "
            "lesson now lives, relative to your project or the workspace."
        ),
    },
    "quote": {
        "type": "string",
        "minLength": 12,
        "description": (
            "With destination: the line in that file that carries the lesson, "
            "copied exactly. The entry is retired only if the file contains it."
        ),
    },
    "placement": {
        **ANSWER_SCHEMA,
        "description": (
            "review select, place proposed or place trial: where the lesson "
            "goes, as the placement review asks. trial is only for your own "
            "unproven hypothesis, never for what a person told you."
        ),
    },
    "room": {
        "type": "array",
        "items": OPERATION_SCHEMA,
        "description": "review select only, when the review refuses for the cap: make-room operations.",
    },
}




def body_errors(operation: str, params: dict[str, Any]) -> list[str]:
    """Check the body with the validator that owns it, not an approximation.

    Mirrors what the schema says, at the two precisions the schema offers.
    ``add`` names its field, so the exact model applies. ``replace`` names an
    entry by id, and its field is a database fact this layer cannot read, so
    the requirement is that the body is a legal entry of *some* shape; the
    store then validates it against the entry's real field and refuses a
    mismatch there, where the field is known.

    Both call :func:`validate_entry_body`, the store's own definition of a
    legal entry, rather than restating it -- and this is what enforces the
    ``oneOf`` that :meth:`Tool._validate` does not implement.
    """
    body = params.get("body")
    if body is None:
        return []
    if not isinstance(body, dict):
        return ["body must be an object"]
    if operation == "add":
        field = params.get("field")
        if field not in ENTRY_FIELD_MODELS:
            return []  # the field enum already reported it
        try:
            validate_entry_body(str(field), body)
        except ContinuityError as invalid:
            return [str(invalid)]
        return []
    for field in _ONE_FIELD_PER_MODEL:
        try:
            validate_entry_body(field, body)
        except ContinuityError:
            continue
        return []
    return [
        "body does not match any continuity entry shape: " + _body_key_groups()
    ]

