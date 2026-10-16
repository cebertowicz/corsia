"""Corrections' thin compatibility layer over the generic verified ONNX asset runtime."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core import onnx_asset
from core.corrections_input import tokenize
from core.corrections_recording import ClassifierVersion
from core.onnx_asset import AssetError, OnnxAssetSpec

ASSET_ID = "corrections-classifier"
INPUT_POLICY_ID = "modernbert-owner-context-v2-2048"
RSS_LIMIT_BYTES = int(2.5 * 1024**3)
CALL_TIMEOUT_SECONDS = 30.0
LOAD_TIMEOUT_SECONDS = 180.0
IDLE_SECONDS = 10 * 60
_GENERIC_READER = "The corrections classifier could not start on this computer. Retry setup."


@dataclass(frozen=True)
class Classification:
    """The corrections-specific representation of one generic head score."""

    version: ClassifierVersion
    score: float
    label: str
    truncated: bool
    original_tokens: int


@dataclass(frozen=True)
class AssetManifest(onnx_asset.AssetManifest):
    """A corrections manifest that preserves its established public helpers."""

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str | None = None) -> "AssetManifest":
        """Read and validate one manifest against the corrections asset spec."""
        try:
            manifest = onnx_asset.load_manifest(path, CORRECTIONS_SPEC, expected_sha256=expected_sha256)
        except AssetError as error:
            if str(error) == "manifest does not describe the supported corrections classifier asset":
                raise AssetError("manifest does not describe the supported corrections asset") from None
            raise
        result = _as_corrections_manifest(manifest)
        result.classifier_version()
        return result

    def verify_files(self) -> None:
        """Check this corrections asset's files against the generic verifier."""
        super().verify_files(CORRECTIONS_SPEC)

    def classifier_version(self, head: str = "correction") -> ClassifierVersion:
        """Return the corrections version identity for one configured manifest head."""
        try:
            threshold = float(self.heads[head]["threshold"])
        except (KeyError, TypeError) as error:
            raise AssetError(f"manifest has no usable {head} head") from error
        except ValueError as error:
            raise AssetError("manifest has no usable correction threshold") from error
        return ClassifierVersion(self.asset_id, self.version, head, threshold, self.input_policy_id)


def score_input(
    session: Any, np: Any, tokenizer: Any, context: list[tuple[str, str]], owner: str,
) -> tuple[float, bool, int]:
    """Score one corrections input using the correction head's probability."""
    ids, truncated, original = tokenize(tokenizer, context, owner)
    logits = session.run(None, {
        "input_ids": np.asarray([ids], dtype=np.int64),
        "attention_mask": np.ones((1, len(ids)), dtype=np.int64),
    })[0][0]
    return float(np.exp(logits[1]) / np.exp(logits).sum()), truncated, original


def score_request(session: Any, np: Any, tokenizer: Any, request: Mapping[str, Any]) -> tuple[float, bool, int]:
    """Adapt generic request/self-test vectors to the corrections score function."""
    return score_input(session, np, tokenizer, list(request.get("context", [])), str(request["owner"]))


class _CorrectionsPins(Mapping[str, str]):
    """Read the current corrections pin mapping so test and setup overrides remain effective."""

    def __getitem__(self, key: str) -> str:
        from core.corrections_asset_pins import PINNED_MANIFESTS

        return PINNED_MANIFESTS[key]

    def __iter__(self):
        from core.corrections_asset_pins import PINNED_MANIFESTS

        return iter(PINNED_MANIFESTS)

    def __len__(self) -> int:
        from core.corrections_asset_pins import PINNED_MANIFESTS

        return len(PINNED_MANIFESTS)


CORRECTIONS_SPEC = OnnxAssetSpec(
    asset_id=ASSET_ID,
    input_policy_id=INPUT_POLICY_ID,
    required_heads=("correction",),
    min_self_test_vectors=4,
    pins=_CorrectionsPins(),
    display_name="corrections classifier",
    labels={"correction": ("not_correction", "correction")},
    rss_limit_bytes=RSS_LIMIT_BYTES,
    load_timeout_seconds=LOAD_TIMEOUT_SECONDS,
    call_timeout_seconds=CALL_TIMEOUT_SECONDS,
    idle_seconds=IDLE_SECONDS,
    score=score_request,
)
_SELF_TEST_LOCK = onnx_asset._SELF_TEST_LOCKS.setdefault(ASSET_ID, threading.Lock())


def asset_root() -> Path:
    """Return the app-owned corrections asset location."""
    overridden = os.environ.get("CORSIA_CORRECTIONS_ASSET_DIR")
    if overridden:
        return Path(overridden)
    from infra.config import STATE_ROOT

    return STATE_ROOT / "assets" / ASSET_ID


def cleanup_staging() -> None:
    """Discard the corrections asset's interrupted installation directory."""
    onnx_asset.cleanup_staging(asset_root())


def open_session(manifest: AssetManifest, ort: Any) -> Any:
    """Create the established CPU session for a corrections manifest."""
    return onnx_asset.open_session(manifest, ort)


def load_tokenizer(manifest: AssetManifest) -> Any:
    """Load the tokenizer packaged beside this corrections manifest."""
    return onnx_asset.load_tokenizer(manifest)


def check_self_test(manifest: AssetManifest, session: Any, np: Any, tokenizer: Any) -> None:
    """Check all corrections vectors through the generic self-test owner."""
    onnx_asset.check_self_test(manifest, CORRECTIONS_SPEC, session, np, tokenizer)


def load_pinned_manifest(asset_dir: Path) -> AssetManifest:
    """Load a source-pinned corrections manifest through the generic verifier."""
    manifest = _as_corrections_manifest(onnx_asset.load_pinned_manifest(asset_dir, CORRECTIONS_SPEC))
    manifest.classifier_version()
    return manifest


def run_self_test(asset_dir: Path) -> None:
    """Prove an installed corrections asset can load and reproduce its scores."""
    onnx_asset.run_self_test(asset_dir, CORRECTIONS_SPEC)


def _as_corrections_manifest(manifest: onnx_asset.AssetManifest) -> AssetManifest:
    return AssetManifest(
        path=manifest.path,
        asset_id=manifest.asset_id,
        version=manifest.version,
        files=manifest.files,
        heads=manifest.heads,
        input_policy_id=manifest.input_policy_id,
        self_test=manifest.self_test,
    )
