"""Verified local ONNX assets shared by bounded classifier runtimes."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from loguru import logger

_HASH_CHUNK_BYTES = 1024 * 1024
_VERIFIED: dict[Path, tuple[tuple[str, int, int], ...]] = {}
_SELF_TEST_LOCKS: dict[str, threading.Lock] = {}

ScoreFunction = Callable[[Any, Any, Any, Mapping[str, Any]], tuple[float, bool, int]]


class AssetError(RuntimeError):
    """A local ONNX asset cannot safely be used."""

    def __init__(self, message: str, *, reader: str | None = None):
        super().__init__(message)
        self.reader = reader


@dataclass(frozen=True)
class OnnxAssetSpec:
    """Per-asset policy and runtime limits for one verified ONNX classifier."""

    asset_id: str
    input_policy_id: str
    required_heads: tuple[str, ...]
    min_self_test_vectors: int
    pins: Mapping[str, str]
    display_name: str
    labels: Mapping[str, tuple[str, str]]
    rss_limit_bytes: int
    load_timeout_seconds: float
    call_timeout_seconds: float
    idle_seconds: float
    score: ScoreFunction


@dataclass(frozen=True)
class AssetManifest:
    """A parsed ONNX asset manifest whose identity is validated against a spec."""

    path: Path
    asset_id: str
    version: str
    files: Mapping[str, Mapping[str, Any]]
    heads: Mapping[str, Mapping[str, Any]]
    input_policy_id: str
    self_test: tuple[Mapping[str, Any], ...]

    def verify_files(self, spec: OnnxAssetSpec) -> None:
        """Check listed files against their size and hash, caching unchanged stats."""
        root = self.path.parent
        stats = []
        for name, details in self.files.items():
            candidate = root / name
            if not candidate.is_file() or candidate.stat().st_size != int(details["bytes"]):
                raise AssetError(
                    f"asset file {name} is absent or has the wrong size",
                    reader=_download_reader(spec, f"is incomplete ({_file_label(name)} is missing or has the wrong size)"),
                )
            stat = candidate.stat()
            stats.append((name, stat.st_size, stat.st_mtime_ns))
        if _VERIFIED.get(self.path) == tuple(stats):
            return
        _VERIFIED.pop(self.path, None)
        for name, details in self.files.items():
            digest = hashlib.sha256()
            with (root / name).open("rb") as handle:
                while chunk := handle.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
            if digest.hexdigest() != details["sha256"]:
                raise AssetError(
                    f"asset file {name} has a sha256 mismatch",
                    reader=_download_reader(spec, f"is damaged ({_file_label(name)} does not match its checksum)"),
                )
        _VERIFIED[self.path] = tuple(stats)


def load_manifest(
    path: Path, spec: OnnxAssetSpec, *, expected_sha256: str | None = None,
) -> AssetManifest:
    """Read a manifest, check an optional pin, and validate its spec-owned policy."""
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and actual != expected_sha256:
        raise AssetError(
            "manifest sha256 does not match the source pin",
            reader=_download_reader(spec, "is not the version this app expects"),
        )
    try:
        value = json.loads(raw)
        manifest = AssetManifest(
            path=path,
            asset_id=str(value["asset_id"]),
            version=str(value["version"]),
            files=value["files"],
            heads={head["name"]: head for head in value["heads"]},
            input_policy_id=str(value["input_policy_id"]),
            self_test=tuple(value["self_test"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AssetError(
            "manifest has an invalid asset shape",
            reader=_download_reader(spec, "is damaged (its description is unreadable)"),
        ) from error
    _validate_manifest(manifest, spec)
    return manifest


def load_pinned_manifest(asset_dir: Path, spec: OnnxAssetSpec) -> AssetManifest:
    """Load a manifest only when its declared version has a matching source pin."""
    path = asset_dir / "manifest.json"
    if not path.is_file():
        raise AssetError("asset is absent", reader=_download_reader(spec, "is incomplete (its description is missing)"))
    declared = load_manifest(path, spec)
    pin = spec.pins.get(declared.version)
    if pin is None:
        raise AssetError(
            f"asset version {declared.version} is not pinned in source",
            reader=f"The {spec.display_name} is a version this app does not recognise. Update the app, then retry setup.",
        )
    manifest = load_manifest(path, spec, expected_sha256=pin)
    if manifest.version != declared.version:
        raise AssetError("asset manifest changed while it was being read")
    return manifest


def cleanup_staging(root: Path) -> None:
    """Discard an interrupted install so only complete asset roots are loadable."""
    staging = root.with_name(f"{root.name}.staging")
    if staging.exists():
        shutil.rmtree(staging)


def open_session(manifest: AssetManifest, ort: Any) -> Any:
    """Create the single CPU ONNX session used by setup and the isolated child."""
    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 4
    model_name = next(name for name in manifest.files if name.endswith(".onnx"))
    return ort.InferenceSession(
        str(manifest.path.parent / model_name), options, providers=["CPUExecutionProvider"],
    )


def load_tokenizer(manifest: AssetManifest) -> Any:
    """Load the tokenizer packaged beside an asset manifest."""
    from tokenizers import Tokenizer

    return Tokenizer.from_file(str(manifest.path.parent / "tokenizer.json"))


def check_self_test(manifest: AssetManifest, spec: OnnxAssetSpec, session: Any, np: Any, tokenizer: Any) -> None:
    """Raise when the spec scorer cannot reproduce every declared self-test score."""
    for vector in manifest.self_test:
        score, _, _ = spec.score(session, np, tokenizer, vector)
        if abs(score - float(vector["expected_score"])) > 1e-4:
            raise AssetError(
                "asset self-test did not reproduce the manifest score",
                reader=f"The {spec.display_name} gave unexpected answers on its built-in checks. Retry setup.",
            )


def run_self_test(asset_dir: Path, spec: OnnxAssetSpec) -> None:
    """Load and self-test one installed asset in this process without a partial pass."""
    lock = _SELF_TEST_LOCKS.setdefault(spec.asset_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise AssetError(f"The {spec.display_name} is already being checked. Try again in a moment.")
    session = None
    try:
        manifest = load_pinned_manifest(asset_dir, spec)
        manifest.verify_files(spec)
        import numpy as np
        import onnxruntime as ort

        session = open_session(manifest, ort)
        check_self_test(manifest, spec, session, np, load_tokenizer(manifest))
    except AssetError as error:
        logger.warning("{} self-test refused the asset: {}", spec.display_name, error)
        raise AssetError(error.reader or _generic_reader(spec)) from None
    except Exception as error:
        logger.exception("{} self-test failed: {}: {}", spec.display_name, type(error).__name__, error)
        raise AssetError(_generic_reader(spec)) from None
    finally:
        del session
        lock.release()


def _validate_manifest(manifest: AssetManifest, spec: OnnxAssetSpec) -> None:
    if manifest.asset_id != spec.asset_id or manifest.input_policy_id != spec.input_policy_id:
        raise AssetError(f"manifest does not describe the supported {spec.display_name} asset")
    if any(head not in manifest.heads for head in spec.required_heads):
        raise AssetError("manifest is missing a required head")
    if len(manifest.self_test) < spec.min_self_test_vectors:
        raise AssetError("manifest is missing required self-test vectors")


def _download_reader(spec: OnnxAssetSpec, detail: str) -> str:
    return f"The {spec.display_name} download {detail}. Retry setup."


def _generic_reader(spec: OnnxAssetSpec) -> str:
    return f"The {spec.display_name} could not start on this computer. Retry setup."


def _file_label(name: str) -> str:
    if name.endswith((".onnx", ".onnx.data")):
        return "the model file"
    if name.startswith("tokenizer"):
        return "a tokenizer file"
    return "a licence file" if name.upper().startswith(("LICENSE", "NOTICE")) else "a file"
