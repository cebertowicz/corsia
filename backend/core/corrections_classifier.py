"""Verified local ONNX correction-classifier asset and isolated runtime.

The runtime deliberately has one asset owner: the manifest supplies model identity,
input policy and heads; callers receive that immutable version with every result.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import threading
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
LOAD_TIMEOUT_SECONDS = 180.0  # cold start: verify, load and self-test, apart from the per-call bound
IDLE_SECONDS = 10 * 60
_HASH_CHUNK_BYTES = 1024 * 1024
# One in-process self-test at a time: a second would load a second model session.
_SELF_TEST_LOCK = threading.Lock()
# Verified file stats per manifest: a file is re-hashed only when its stat changes,
# so a call never re-reads the whole model.
_VERIFIED: dict[Path, tuple[tuple[str, int, int], ...]] = {}


class AssetError(RuntimeError):
    """A local classifier asset cannot safely be used.

    `reader` is the sentence a person may be shown; the message stays technical.
    """

    def __init__(self, message: str, *, reader: str | None = None):
        super().__init__(message)
        self.reader = reader


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
            raise AssetError(
                "manifest sha256 does not match the source pin",
                reader="The corrections classifier download is not the version this app expects. "
                "Retry setup.",
            )
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
            raise AssetError(
                "manifest has an invalid classifier shape",
                reader="The corrections classifier download is damaged (its description is unreadable). "
                "Retry setup.",
            ) from error
        if manifest.asset_id != ASSET_ID or manifest.input_policy_id != INPUT_POLICY_ID:
            raise AssetError("manifest does not describe the supported corrections asset")
        if "correction" not in manifest.heads or len(manifest.self_test) != 4:
            raise AssetError("manifest is missing the correction head or four self-test vectors")
        return manifest

    def verify_files(self) -> None:
        root = self.path.parent
        stats = []
        for name, details in self.files.items():
            candidate = root / name
            if not candidate.is_file() or candidate.stat().st_size != int(details["bytes"]):
                raise AssetError(
                    f"asset file {name} is absent or has the wrong size",
                    reader=f"The corrections classifier download is incomplete ({_file_label(name)} "
                    "is missing or has the wrong size). Retry setup.",
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
                    reader=f"The corrections classifier download is damaged ({_file_label(name)} "
                    "does not match its checksum). Retry setup.",
                )
        _VERIFIED[self.path] = tuple(stats)

    def checker_version(self, head: str = "correction") -> ClassifierVersion:
        try:
            threshold = float(self.heads[head]["threshold"])
        except (KeyError, TypeError, ValueError) as error:
            raise AssetError(f"manifest has no usable {head} head") from error
        return ClassifierVersion(self.asset_id, self.version, head, threshold, self.input_policy_id)


def _file_label(name: str) -> str:
    if name.endswith((".onnx", ".onnx.data")):
        return "the model file"
    if name.startswith("tokenizer"):
        return "a tokenizer file"
    return "a licence file" if name.upper().startswith(("LICENSE", "NOTICE")) else "a file"


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


def _disable_child_network() -> None:
    """Inference is local-only: prohibit every outbound socket before ORT imports."""
    import socket

    def blocked_connect(*_args, **_kwargs):
        raise OSError("corrections classifier child prohibits outbound connections")

    setattr(socket.socket, "connect", blocked_connect)
    os.environ["ORT_DISABLE_TELEMETRY_EVENTS"] = "1"


def _open_session(manifest: AssetManifest, ort: Any) -> Any:
    """One CPU session with the options both the child and the setup self-test use."""
    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 4
    model_name = next(name for name in manifest.files if name.endswith(".onnx"))
    return ort.InferenceSession(
        str(manifest.path.parent / model_name), options, providers=["CPUExecutionProvider"],
    )


def _tokenizer_path(manifest: AssetManifest) -> str:
    return str(manifest.path.parent / "tokenizer.json")


def _score(
    session: Any, np: Any, tokenizer_path: str, context: list[tuple[str, str]], owner: str,
) -> tuple[float, bool, int]:
    ids, mask, truncated, original = _tokenize(tokenizer_path, context, owner)
    logits = session.run(None, {
        "input_ids": np.asarray([ids], dtype=np.int64),
        "attention_mask": np.asarray([mask], dtype=np.int64),
    })[0][0]
    return float(np.exp(logits[1]) / np.exp(logits).sum()), truncated, original


def _check_self_test(manifest: AssetManifest, session: Any, np: Any, tokenizer_path: str) -> None:
    for vector in manifest.self_test:
        score, _, _ = _score(
            session, np, tokenizer_path, list(vector.get("context", [])), str(vector["owner"]),
        )
        if abs(score - float(vector["expected_score"])) > 1e-4:
            raise AssetError(
                "classifier self-test did not reproduce the manifest score",
                reader="The corrections classifier gave unexpected answers on its built-in checks. "
                "Retry setup.",
            )


def _readable(sentence: str) -> AssetError:
    return AssetError(sentence, reader=sentence)


def load_pinned_manifest(asset_dir: Path) -> AssetManifest:
    """Load an asset's manifest only when its bytes match the source pin for the version it declares.

    The pin is keyed by the manifest's own version, never by the directory name, so the
    installed location can be named anything and an unpinned asset is always refused.
    """
    from core.corrections_asset_pins import PINNED_MANIFESTS

    path = asset_dir / "manifest.json"
    if not path.is_file():
        raise AssetError(
            "classifier asset is absent",
            reader="The corrections classifier download is incomplete (its description is missing). "
            "Retry setup.",
        )
    declared = AssetManifest.load(path)
    pin = PINNED_MANIFESTS.get(declared.version)
    if pin is None:
        raise AssetError(
            f"classifier version {declared.version} is not pinned in source",
            reader="The corrections classifier is a version this app does not recognise. "
            "Update the app, then retry setup.",
        )
    manifest = AssetManifest.load(path, expected_sha256=pin)
    if manifest.version != declared.version:
        raise AssetError("classifier manifest changed while it was being read")
    return manifest


def run_self_test(asset_dir: Path) -> None:
    """Prove an installed asset loads and reproduces its manifest scores, in this process.

    Raises AssetError with a sentence a person can read; never returns a partial pass.
    """
    if not _SELF_TEST_LOCK.acquire(blocking=False):
        raise _readable("The corrections classifier is already being checked. Try again in a moment.")
    session = None
    try:
        manifest = load_pinned_manifest(asset_dir)
        manifest.verify_files()
        import numpy as np
        import onnxruntime as ort

        session = _open_session(manifest, ort)
        _check_self_test(manifest, session, np, _tokenizer_path(manifest))
    except AssetError as error:
        # str() is the person-readable sentence; the technical message stays internal.
        raise AssetError(error.reader or _GENERIC_READER) from None
    except Exception:
        raise AssetError(_GENERIC_READER) from None
    finally:
        del session
        _SELF_TEST_LOCK.release()


_GENERIC_READER = "The corrections classifier could not start on this computer. Retry setup."


def _child_main(connection, manifest_path: str) -> None:
    """Own one ORT session. It exits after a ceiling breach, never above it."""
    try:
        _disable_child_network()
        import numpy as np
        import onnxruntime as ort

        manifest = load_pinned_manifest(Path(manifest_path).parent)
        manifest.verify_files()
        session = _open_session(manifest, ort)
        tokenizer_path = _tokenizer_path(manifest)
        _check_self_test(manifest, session, np, tokenizer_path)
        connection.send({"ready": True})
        while connection.poll(IDLE_SECONDS):
            request = connection.recv()
            if request == {"op": "close"}:
                return
            head = str(request.get("head", "correction"))
            version = manifest.checker_version(head)
            score, truncated, original = _score(
                session, np, tokenizer_path, request["context"], request["owner"],
            )
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
        self._loaded = False
        self.last_used = 0.0

    def _start(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        parent, child = Pipe()
        self._process = Process(target=_child_main, args=(child, str(self._manifest.path)), daemon=True)
        self._process.start()
        self._connection, self._loaded = parent, False

    def _exchange(self, request: dict | None, timeout: float, timeout_reason: str) -> dict:
        assert self._connection is not None
        try:
            if request is not None:
                self._connection.send(request)
            reply = self._connection.recv() if self._connection.poll(timeout) else {"error": timeout_reason}
        except (EOFError, OSError):
            reply = {"error": "child_exited"}
        if "error" in reply:
            self.close(kill=True)
            raise ClassifierUnavailable(str(reply["error"]))
        return reply

    def classify(self, context: list[tuple[str, str]], owner: str, *, head: str = "correction") -> Classification:
        self._start()
        if not self._loaded:
            self._loaded = bool(self._exchange(None, LOAD_TIMEOUT_SECONDS, "load_timeout"))
        reply = self._exchange({"context": context, "owner": owner, "head": head}, CALL_TIMEOUT_SECONDS, "timeout")
        self.last_used = time.monotonic()
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
