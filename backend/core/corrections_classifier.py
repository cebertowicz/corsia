"""Corrections-specific configuration over the generic verified ONNX runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.corrections_asset_pins import PINNED_MANIFESTS
from core.corrections_input import tokenize
from core.corrections_recording import ClassifierVersion
from core.onnx_asset import OnnxAssetSpec


@dataclass(frozen=True)
class Classification:
    """The corrections-specific representation of one generic head score."""

    version: ClassifierVersion
    score: float
    label: str
    truncated: bool
    original_tokens: int


def score_request(session: Any, np: Any, tokenizer: Any, request: Mapping[str, Any]) -> tuple[float, bool, int]:
    """Score a generic corrections request with the correction head's probability."""
    context = list(request.get("context", []))
    ids, truncated, original = tokenize(tokenizer, context, str(request["owner"]))
    logits = session.run(None, {
        "input_ids": np.asarray([ids], dtype=np.int64),
        "attention_mask": np.ones((1, len(ids)), dtype=np.int64),
    })[0][0]
    return float(np.exp(logits[1]) / np.exp(logits).sum()), truncated, original


CORRECTIONS_SPEC = OnnxAssetSpec(
    asset_id="corrections-classifier",
    input_policy_id="modernbert-owner-context-v2-2048",
    required_heads=("correction",),
    min_self_test_vectors=4,
    pins=PINNED_MANIFESTS,
    display_name="corrections classifier",
    labels={"correction": ("not_correction", "correction")},
    rss_limit_bytes=int(2.5 * 1024**3),
    load_timeout_seconds=180.0,
    call_timeout_seconds=30.0,
    idle_seconds=10 * 60,
    score=score_request,
)


def classification_from_reply(reply: Mapping[str, Any]) -> Classification:
    """Turn one generic corrections-head reply into the existing ``Classification`` value."""
    version = ClassifierVersion(**reply["version"])
    negative, positive = CORRECTIONS_SPEC.labels[version.head]
    score = float(reply["score"])
    return Classification(
        version=version,
        score=score,
        label=positive if score >= version.threshold else negative,
        truncated=bool(reply["truncated"]),
        original_tokens=int(reply["original_tokens"]),
    )
