"""Verified local ONNX correction-classifier asset and isolated runtime.

The runtime deliberately has one asset owner: the manifest supplies model identity,
input policy and heads; callers receive that immutable version with every result.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import time
from dataclasses import dataclass
from multiprocessing import Pipe, Process
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Mapping

ASSET_ID = "corrections-classifier"
INPUT_POLICY_ID = "modernbert-owner-context-v2-2048"
MAX_OWNER_TOKENS = 1024
MAX_INPUT_TOKENS = 2048
RSS_LIMIT_BYTES = int(2.5 * 1024**3)
CALL_TIMEOUT_SECONDS = 30.0
IDLE_SECONDS = 10 * 60


class AssetError(RuntimeError):
    """A local classifier asset cannot safely be used."""


class ClassifierUnavailable(RuntimeError):
    """The worker should leave a message pending and try on a later arrival."""


@dataclass(frozen=True)
class ClassifierVersion:
    asset_id: str
    version: str
    head: str
    threshold: float
    input_policy_id: str


@dataclass(frozen=True)
class Classification:
    version: ClassifierVersion
    score: float
    label: str
    truncated: bool
    original_tokens: int


@dataclass(frozen=True)
class AssetManifest:
    path: Path
    asset_id: str
    version: str
    files: Mapping[str, Mapping[str, Any]]
    heads: Mapping[str, Mapping[str, Any]]
    input_policy_id: str
    self_test: tuple[Mapping[str, Any], ...]

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str | None = None) -> "AssetManifest":
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if expected_sha256 and actual != expected_sha256:
            raise AssetError("manifest sha256 does not match the source pin")
        try:
            value = json.loads(raw)
            files = value["files"]
            heads = {head["name"]: head for head in value["heads"]}
            manifest = cls(
                path=path,
                asset_id=str(value["asset_id"]),
                version=str(value["version"]),
                files=files,
                heads=heads,
                input_policy_id=str(value["input_policy_id"]),
                self_test=tuple(value["self_test"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise AssetError("manifest has an invalid classifier shape") from error
        if manifest.asset_id != ASSET_ID or manifest.input_policy_id != INPUT_POLICY_ID:
            raise AssetError("manifest does not describe the supported corrections asset")
        if "correction" not in manifest.heads or len(manifest.self_test) != 4:
            raise AssetError("manifest is missing the correction head or four self-test vectors")
        return manifest

    def verify_files(self) -> None:
        root = self.path.parent
        for name, details in self.files.items():
            candidate = root / name
            if not candidate.is_file() or candidate.stat().st_size != int(details["bytes"]):
                raise AssetError(f"asset file {name} is absent or has the wrong size")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != details["sha256"]:
                raise AssetError(f"asset file {name} has a sha256 mismatch")

    def checker_version(self, head: str = "correction") -> ClassifierVersion:
        try:
            threshold = float(self.heads[head]["threshold"])
        except (KeyError, TypeError, ValueError) as error:
            raise AssetError(f"manifest has no usable {head} head") from error
        return ClassifierVersion(self.asset_id, self.version, head, threshold, self.input_policy_id)


def asset_root() -> Path:
    """Return the app-owned location; tests may override only their disposable root."""
    overridden = os.environ.get("CORSIA_CORRECTIONS_ASSET_DIR")
    if overridden:
        return Path(overridden)
    from infra.config import STATE_ROOT

    return STATE_ROOT / "assets" / ASSET_ID


def build_text(context: list[tuple[str, str]], owner_message: str) -> str:
    """The exact v2 training format: context first, owner message last."""
    parts = [f"[{role}] {text}" for role, text in context]
    parts.append(f"[OWNER] {owner_message}")
    return "\n".join(parts)


def _tokenize(tokenizer_path: str, context: list[tuple[str, str]], owner: str) -> tuple[list[int], list[int], bool, int]:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(tokenizer_path)
    owner_ids = tokenizer.encode(f"[OWNER] {owner}", add_special_tokens=False).ids
    cap = min(MAX_INPUT_TOKENS, max(MAX_OWNER_TOKENS, len(owner_ids) + 2))
    encoded = tokenizer.encode(build_text(context, owner), add_special_tokens=True)
    original_tokens = len(encoded.ids)
    ids = encoded.ids[-cap:]
    mask = [1] * len(ids)
    return ids, mask, original_tokens > cap, original_tokens


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if os.uname().sysname == "Darwin" else value * 1024


def _child_main(connection, manifest_path: str) -> None:
    """Own one ORT session. It exits after a ceiling breach, never above it."""
    try:
        import numpy as np
        import onnxruntime as ort

        manifest = AssetManifest.load(Path(manifest_path))
        manifest.verify_files()
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 4
        model_name = next(name for name in manifest.files if name.endswith(".onnx"))
        session = ort.InferenceSession(
            str(manifest.path.parent / model_name), options, providers=["CPUExecutionProvider"],
        )
        tokenizer_path = str(manifest.path.parent / "tokenizer.json")
        for vector in manifest.self_test:
            ids, mask, _, _ = _tokenize(
                tokenizer_path, list(vector.get("context", [])), str(vector["owner"]),
            )
            logits = session.run(None, {
                "input_ids": np.asarray([ids], dtype=np.int64),
                "attention_mask": np.asarray([mask], dtype=np.int64),
            })[0][0]
            score = float(np.exp(logits[1]) / np.exp(logits).sum())
            if abs(score - float(vector["expected_score"])) > 1e-4:
                raise AssetError("classifier self-test did not reproduce the manifest score")
        while connection.poll(IDLE_SECONDS):
            request = connection.recv()
            if request == {"op": "close"}:
                return
            head = str(request.get("head", "correction"))
            version = manifest.checker_version(head)
            ids, mask, truncated, original = _tokenize(tokenizer_path, request["context"], request["owner"])
            arrays = {
                "input_ids": np.asarray([ids], dtype=np.int64),
                "attention_mask": np.asarray([mask], dtype=np.int64),
            }
            logits = session.run(None, arrays)[0][0]
            score = float(np.exp(logits[1]) / np.exp(logits).sum())
            if _peak_rss_bytes() > RSS_LIMIT_BYTES:
                connection.send({"error": "rss"})
                return
            connection.send({
                "score": score, "version": version.__dict__, "truncated": truncated,
                "original_tokens": original,
            })
    except Exception as error:
        connection.send({"error": f"error:{type(error).__name__}"})


class ClassifierChild:
    """Synchronous parent for the short-lived, serial model child process."""

    def __init__(self, manifest: AssetManifest):
        self._manifest = manifest
        self._process: Process | None = None
        self._connection: Connection[Any, Any] | None = None
        self.last_used = 0.0

    def _start(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        parent, child = Pipe()
        self._process = Process(target=_child_main, args=(child, str(self._manifest.path)), daemon=True)
        self._process.start()
        self._connection = parent

    def classify(self, context: list[tuple[str, str]], owner: str, *, head: str = "correction") -> Classification:
        self._start()
        assert self._connection is not None and self._process is not None
        self._connection.send({"context": context, "owner": owner, "head": head})
        if not self._connection.poll(CALL_TIMEOUT_SECONDS):
            self.close(kill=True)
            raise ClassifierUnavailable("timeout")
        reply = self._connection.recv()
        self.last_used = time.monotonic()
        if "error" in reply:
            self.close()
            raise ClassifierUnavailable(str(reply["error"]))
        version = ClassifierVersion(**reply["version"])
        return Classification(
            version=version,
            score=float(reply["score"]),
            label="correction" if float(reply["score"]) >= version.threshold else "not_correction",
            truncated=bool(reply["truncated"]),
            original_tokens=int(reply["original_tokens"]),
        )

    def close(self, *, kill: bool = False) -> None:
        if self._process is None:
            return
        if self._process.is_alive() and kill:
            self._process.kill()
        elif self._process.is_alive() and self._connection is not None:
            try:
                self._connection.send({"op": "close"})
            except (BrokenPipeError, EOFError):
                pass
        self._process.join(timeout=1)
        self._process = None
        self._connection = None
