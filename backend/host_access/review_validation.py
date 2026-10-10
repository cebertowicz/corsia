"""Bounded, duplicate-free host verdict validation with strict core fields."""
from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from host_access.failure_reasons import HostAccessReviewValidationError
from llm.isolated_structured import _strict_json_loads


HOST_ACCESS_REVIEW_MAX_ENCODED_BYTES = 8 * 1024
ReviewPayload = TypeVar("ReviewPayload", bound=BaseModel)


def validate_review_payload(
    raw: object,
    model: type[ReviewPayload],
) -> ReviewPayload:
    try:
        if isinstance(raw, (str, bytes)):
            payload = _strict_json_loads(raw)
        else:
            payload = raw
        if not isinstance(payload, dict):
            raise ValueError("review payload is not an object")
        if type(payload.get("schema_version")) is not int:
            raise ValueError("review schema version must be an integer")
        # Keep the required verdict strict; echoed request metadata is not policy.
        core = {key: value for key, value in payload.items() if key in model.model_fields}
        validated = model.model_validate(core, strict=True)
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise HostAccessReviewValidationError(
            "host-access review payload is not strict schema JSON"
        ) from exc
    if len(encoded) > HOST_ACCESS_REVIEW_MAX_ENCODED_BYTES:
        raise HostAccessReviewValidationError(
            "host-access review payload exceeds the encoded-size limit"
        )
    return validated
