"""Serialize the durable application-review envelope and its provenance."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from host_access.review_models import ApplicationReview


def application_review_record(review: ApplicationReview) -> dict[str, Any]:
    return {
        "schema_version": review.schema_version,
        "verdict": review.verdict,
        "reason_category": review.reason_category,
        "reason": review.reason,
        "attestation": review.user_attestation,
        "reviewer_display_name": review.identity.display_name,
        "reviewer_reported_model": review.identity.reported_model,
        "reviewer_harness": review.identity.harness,
        "reviewer_transport_model": review.identity.transport_model,
        "reviewer_fallback": review.reviewer_fallback,
    }


def encode_application_review(review_result: dict[str, Any], *, auto_approve: bool) -> str:
    # CORSIA-owned provenance wins over a colliding model field.
    return json.dumps(
        {**review_result, "approval_mode": "automatic" if auto_approve else "manual"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
