"""Corrections compatibility wrapper over the generic isolated ONNX child lifecycle."""

from __future__ import annotations

from typing import Any, Callable

from core.corrections_classifier import AssetManifest, CORRECTIONS_SPEC, Classification
from core import onnx_child

RSS_LIMIT_BYTES = CORRECTIONS_SPEC.rss_limit_bytes
CALL_TIMEOUT_SECONDS = CORRECTIONS_SPEC.call_timeout_seconds
LOAD_TIMEOUT_SECONDS = CORRECTIONS_SPEC.load_timeout_seconds
IDLE_SECONDS = CORRECTIONS_SPEC.idle_seconds


class ClassifierUnavailable(RuntimeError):
    """The corrections model could not safely load; the message remains pending."""

    def __init__(self, reason: str, *, kind: str = "transient", cause: str | None = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.cause = cause


class MessageFailed(RuntimeError):
    """A ready corrections model failed for one input and must be restarted."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _disable_child_network() -> None:
    """Preserve the corrections test seam for the generic child network block."""
    onnx_child._disable_child_network()


def _peak_rss_bytes() -> int:
    """Preserve the corrections test seam for the child-side peak RSS check."""
    return onnx_child._peak_rss_bytes()


def _child_main(connection: Any, manifest_path: str) -> None:
    """Serve corrections through the generic child using its declared asset spec."""
    onnx_child.child_main(connection, manifest_path, CORRECTIONS_SPEC)


def _compat_target(connection: Any, manifest_path: str, _spec: Any, target: Callable[..., None]) -> None:
    """Adapt the historical two-argument target injection seam used by corrections tests."""
    target(connection, manifest_path)


class ClassifierChild(onnx_child.OnnxChild):
    """Corrections result adapter retaining the established parent API and failures."""

    def __init__(self, manifest: AssetManifest, *, target: Callable[..., None] = _child_main):
        if target is _child_main:
            super().__init__(manifest, CORRECTIONS_SPEC)
        else:
            super().__init__(manifest, CORRECTIONS_SPEC, target=_compat_target, target_args=(target,))

    def ensure_loaded(self) -> None:
        """Start the verified child and map generic load failures to corrections failures."""
        try:
            super().ensure_loaded()
        except onnx_child.OnnxUnavailable as error:
            raise ClassifierUnavailable(str(error), kind=error.kind, cause=error.cause) from None

    def classify(
        self, context: list[tuple[str, str]], owner: str, *, head: str = "correction",
    ) -> Classification:
        """Score one corrections request and turn the generic reply into ``Classification``."""
        try:
            reply = self.score({"context": list(context), "owner": owner, "head": head})
        except onnx_child.OnnxMessageFailed as error:
            raise MessageFailed(error.reason) from None
        except onnx_child.OnnxUnavailable as error:
            if str(error) in ("closed", "not loaded"):
                raise ClassifierUnavailable(str(error)) from None
            raise MessageFailed("child_error") from None
        version = self._manifest.classifier_version(str(reply["version"]["head"]))
        score = float(reply["score"])
        negative, positive = CORRECTIONS_SPEC.labels[head]
        return Classification(
            version=version,
            score=score,
            label=positive if score >= version.threshold else negative,
            truncated=bool(reply["truncated"]),
            original_tokens=int(reply["original_tokens"]),
        )


CLOSE_JOIN_SECONDS = onnx_child.CLOSE_JOIN_SECONDS
