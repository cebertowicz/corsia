"""Opt-in controls for the canonical held-document E2E recovery proof.

Only the test app and tests import this module. Mutation controls require all
live-fault gates and resolve an already loaded ``LiveEntry`` through its real
lane. Evidence decodes read-only CRDT snapshots; it owns no alternate writer.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException

FAULT_STAGES = frozenset({"choice_record", "choice_rename", "choice_apply", "choice_clear"})
_REFUSALS = frozenset({"commit", "record", "apply", "rename", "clear"})
_active_artifact: contextvars.ContextVar[str | None] = contextvars.ContextVar("e2e_live_fault_artifact", default=None)


class FaultConfigurationError(ValueError):
    """The caller did not establish the isolated fault-test boundary."""


class HeldPrepared(RuntimeError):
    """Expected test-only interruption after canonical pending-record storage."""


@dataclass(frozen=True)
class FaultConfiguration:
    run_id: str
    workspace: Path
    state_dir: Path
    run_dir: Path
    marker_path: Path
    restart_path: Path

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> "FaultConfiguration":
        env = os.environ if environ is None else environ
        if env.get("CORSIA_E2E_LIVE_FAULTS") != "1":
            raise FaultConfigurationError("CORSIA_E2E_LIVE_FAULTS=1 is required")
        missing = [name for name in ("CORSIA_TEST_MODE", "CORSIA_E2E_MOCK", "CORSIA_E2E_FRESH_ROOTS") if env.get(name) != "1"]
        if missing:
            raise FaultConfigurationError("live faults require " + ", ".join(f"{name}=1" for name in missing))
        run_id = env.get("CORSIA_E2E_LIVE_FAULT_RUN_ID", "")
        workspace_text = env.get("CORSIA_WORKSPACE", "")
        state_text = env.get("CORSIA_STATE_DIR", "")
        run_text = env.get("CORSIA_E2E_LIVE_FAULT_RUN_DIR", "")
        if not run_id or not workspace_text or not state_text or not run_text:
            raise FaultConfigurationError("live faults require a supervisor run identity and isolated workspace/state roots")
        workspace, state_dir, run_dir = (Path(value).resolve() for value in (workspace_text, state_text, run_text))
        if workspace == state_dir or not workspace.is_dir() or not state_dir.is_dir() or not run_dir.is_dir():
            raise FaultConfigurationError("live faults require prepared, distinct workspace and state roots")
        try:
            run_dir.relative_to(state_dir)
        except ValueError as exc:
            raise FaultConfigurationError("live-fault control directory must be inside CORSIA_STATE_DIR") from exc
        identity = run_dir / "identity.json"
        expected = {"run_id": run_id, "workspace": str(workspace), "state_dir": str(state_dir)}
        try:
            recorded = json.loads(identity.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise FaultConfigurationError("live-fault supervisor identity is missing or unreadable") from exc
        if recorded != expected:
            raise FaultConfigurationError("live-fault supervisor identity does not match these isolated roots")
        return cls(run_id, workspace, state_dir, run_dir, run_dir / "armed-cut.json", run_dir / "restart.json")


class FaultController:
    """Scoped real-SIGKILL control over one loaded canonical document lane."""

    def __init__(self, configuration: FaultConfiguration):
        self.configuration = configuration
        self._bound: dict[str, object] = {}
        self._preparing: set[str] = set()

    @property
    def pid(self) -> int:
        return os.getpid()

    def _entry(self, artifact_id: str):
        from doceditor.live_runtime import get_documents

        manager = get_documents()
        entry = manager.documents.get(artifact_id) if manager is not None else None
        if entry is None or entry.artifact_id != artifact_id:
            raise HTTPException(409, "Live-fault control requires this artifact's already loaded live entry")
        return entry

    def _bind(self, artifact_id: str):
        entry = self._entry(artifact_id)
        if self._bound.get(artifact_id) is entry:
            return entry
        lane = entry.lane
        original_enqueue = lane._enqueue
        original_execute = lane._execute

        async def execute(operation):
            context = _active_artifact.set(artifact_id)
            try:
                return await original_execute(operation)
            finally:
                _active_artifact.reset(context)

        def enqueue(operation, cancel_event=None, *, recovery=False):
            # Preserve Operation identity through admission and cancellation.
            if not callable(operation):
                return original_enqueue(operation, cancel_event, recovery=recovery)
            async def scoped():
                token = _active_artifact.set(artifact_id)
                try:
                    return await operation()
                finally:
                    _active_artifact.reset(token)
            return original_enqueue(scoped, cancel_event, recovery=recovery)

        lane._execute = execute
        lane._enqueue = enqueue
        self._bound[artifact_id] = entry
        return entry

    def _restart_state(self) -> tuple[str | None, int]:
        try:
            record = json.loads(self.configuration.restart_path.read_text())
            case_id, restart_count = record["case_id"], record["restart_count"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError("live-fault supervisor restart record is missing or unreadable") from exc
        if (case_id is not None and not isinstance(case_id, str)) or type(restart_count) is not int or restart_count not in (0, 1):
            raise RuntimeError("live-fault supervisor restart record is invalid")
        return case_id, restart_count

    def _write_restart_state(self, case_id: str | None, restart_count: int) -> None:
        temporary = self.configuration.restart_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"case_id": case_id, "restart_count": restart_count}, sort_keys=True))
        os.replace(temporary, self.configuration.restart_path)

    def begin_case(self) -> dict[str, object]:
        if self.configuration.marker_path.exists():
            raise HTTPException(409, "Cannot reset a live-fault case while a cut is armed")
        case_id = os.urandom(16).hex()
        self._write_restart_state(case_id, 0)
        return {"case_id": case_id, "restart_count": 0}

    def status(self, artifact_id: str | None = None) -> dict[str, object]:
        case_id, restart_count = self._restart_state()
        detail: dict[str, object] = {"run_id": self.configuration.run_id, "pid": self.pid,
                                     "armed": self.configuration.marker_path.exists(), "case_id": case_id,
                                     "restart_count": restart_count}
        if artifact_id:
            entry = self._entry(artifact_id)
            pending = entry.live.store.pending()
            detail.update({"artifact_id": artifact_id, "store_id": entry.live.store.store_id,
                           "received": len(entry.live.received), "pending": pending.state if pending else None,
                           "received_ids": list(entry.live.received),
                           "choice": pending.choice.kind if pending and pending.choice else None})
        return detail

    async def evidence(self, artifact_id: str) -> dict[str, object]:
        """Read the selected canonical lane/store and exact file bytes at a barrier."""
        from doceditor.live_text import clone
        entry = self._entry(artifact_id)
        async def read():
            async with entry.live.committer_lock:
                updates = await asyncio.to_thread(entry.live.store.load_updates)
                durable = clone(updates[0])
                for update in updates[1:]:
                    durable.apply_update(update)
                mirror = await asyncio.to_thread(entry.live.store.mirror)
                mirrored = clone(mirror.state)
                disk = await asyncio.to_thread(entry.live.path.read_bytes)
                return {**self.status(artifact_id), "live_text": entry.live.text,
                        "durable_text": str(durable["text"]), "mirror_text": str(mirrored["text"]),
                        "file_text": disk.decode("utf-8"),
                        "live_state_digest": hashlib.sha256(entry.live.doc.get_update()).hexdigest(),
                        "durable_state_digest": hashlib.sha256(durable.get_update()).hexdigest(),
                        "mirror_state_digest": hashlib.sha256(mirrored.get_update()).hexdigest()}
        return await entry.lane._submit(read)

    def arm(self, artifact_id: str, stage: str) -> dict[str, object]:
        if stage not in FAULT_STAGES:
            raise HTTPException(404, f"Unknown held-document fault cut: {stage}")
        self._bind(artifact_id)
        if self.configuration.marker_path.exists():
            raise HTTPException(409, "A live-fault cut is already armed for this child")
        case_id, restart_count = self._restart_state()
        if case_id is None or restart_count != 0:
            raise HTTPException(409, "Begin a fresh live-fault case before arming one cut")
        payload = {"run_id": self.configuration.run_id, "pid": self.pid, "case_id": case_id,
                   "artifact_id": artifact_id, "stage": stage, "generation": 1}
        temporary = self.configuration.marker_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.configuration.marker_path)
            directory = os.open(self.configuration.run_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        if stage in {"choice_apply", "choice_clear"}:
            self._transaction_cut(artifact_id, "apply_choice" if stage == "choice_apply" else "clear_choice", stage)
        return payload

    def _transaction_cut(self, artifact_id: str, method: str, stage: str) -> None:
        store = self._entry(artifact_id).live.store
        original = getattr(store, method)
        def cut(*args, **kwargs):
            transaction = store._transaction
            def interrupted(action):
                def inside(db):
                    result = action(db)
                    self.kill_at(stage)
                    return result
                return transaction(inside)
            store._transaction = interrupted
            try:
                return original(*args, **kwargs)
            finally:
                store._transaction = transaction
                setattr(store, method, original)
        setattr(store, method, cut)

    async def checkpoint(self, stage: str) -> None:
        artifact_id = _active_artifact.get()
        if artifact_id in self._preparing and stage == "record":
            raise HeldPrepared("canonical pending record is ready for an outside-file conflict")
        if stage not in {"choice_apply", "choice_clear"}:
            self.kill_at(stage)

    def kill_at(self, stage: str) -> None:
        artifact_id = _active_artifact.get()
        if artifact_id is None:
            return
        try:
            armed = json.loads(self.configuration.marker_path.read_text())
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("live-fault marker became unreadable") from exc
        case_id, restart_count = self._restart_state()
        expected = {"run_id": self.configuration.run_id, "pid": self.pid, "case_id": case_id,
                    "artifact_id": artifact_id, "stage": stage, "generation": restart_count + 1}
        if armed == expected:
            os.kill(self.pid, signal.SIGKILL)

    async def prepare_held(self, artifact_id: str, outside_text: str) -> dict[str, object]:
        """Create a real pending record, external change, and canonical held barrier."""
        from doceditor.document_save import write_atomic_plain
        from doceditor.live_lane import Operation

        entry = self._bind(artifact_id)
        actor = entry.live.viewer_actor()
        if actor is None:
            raise HTTPException(409, "Held preparation requires a current writable viewer actor")
        self._preparing.add(artifact_id)
        try:
            try:
                result = await entry.lane.submit(Operation("replace", content=entry.live.text, actor=actor))
                result.require_landed()
            except HeldPrepared:
                pass
        finally:
            self._preparing.discard(artifact_id)
        pending = entry.live.store.pending()
        if pending is None or pending.state != "pending":
            raise HTTPException(409, "Could not create the canonical pending document record")
        await asyncio.to_thread(write_atomic_plain, entry.live.path, outside_text, pending.prior_hash)
        await entry.lane.recover()
        pending = entry.live.store.pending()
        if pending is None or pending.state != "held":
            raise HTTPException(409, "Canonical recovery did not enter the held barrier")
        return self.status(artifact_id)

    def refuse_once(self, artifact_id: str, kind: str) -> dict[str, object]:
        """Fault exactly one direct canonical call; its normal recovery remains real."""
        if kind not in _REFUSALS:
            raise HTTPException(404, f"Unknown held-document refusal: {kind}")
        entry = self._bind(artifact_id)
        from doceditor.live_store import StoreRefused
        if kind == "rename":
            original = entry.lane.write
            async def rename_refused(text, prior_hash):
                entry.lane.write = original
                raise PermissionError("test-only canonical rename refusal")
            entry.lane.write = rename_refused
        else:
            owner = entry.live.store
            method = {"commit": "commit_batch", "record": "record_choice", "apply": "apply_choice", "clear": "clear_choice"}[kind]
            original = getattr(owner, method)
            def store_refused(*args, **kwargs):
                setattr(owner, method, original)
                raise StoreRefused(f"test-only canonical {kind} refusal")
            setattr(owner, method, store_refused)
        if kind == "clear":
            # clear_choice runs only after a refused rename.
            self.refuse_once(artifact_id, "rename")
        return {"artifact_id": artifact_id, "refusal": kind}

    @contextmanager
    def install_checkpoint_hook(self) -> Iterator[None]:
        from doceditor import live_lane
        original = live_lane._checkpoint
        async def fault_checkpoint(stage: str) -> None:
            await original(stage)
            await self.checkpoint(stage)
        live_lane._checkpoint = fault_checkpoint
        try:
            yield
        finally:
            live_lane._checkpoint = original


def build_router(controller: FaultController | None, *, opt_in_requested: bool) -> APIRouter | None:
    if not opt_in_requested:
        return None
    router = APIRouter(prefix="/api/test/live-faults", tags=["test"])
    def require_controller() -> FaultController:
        if controller is None:
            raise HTTPException(403, "Live-fault controls require mock test mode, fresh roots, and a supervisor identity")
        return controller
    @router.get("/status")
    async def status(artifact_id: str | None = None) -> dict[str, object]:
        return require_controller().status(artifact_id)
    @router.post("/case")
    async def begin_case() -> dict[str, object]:
        return require_controller().begin_case()
    @router.get("/{artifact_id}/evidence")
    async def evidence(artifact_id: str) -> dict[str, object]:
        return await require_controller().evidence(artifact_id)
    @router.post("/{artifact_id}/prepare-held")
    async def prepare_held(artifact_id: str, body: dict) -> dict[str, object]:
        outside_text = body.get("outside_text")
        if not isinstance(outside_text, str):
            raise HTTPException(400, "outside_text is required")
        return await require_controller().prepare_held(artifact_id, outside_text)
    @router.post("/{artifact_id}/refuse/{kind}")
    async def refuse(artifact_id: str, kind: str) -> dict[str, object]:
        return require_controller().refuse_once(artifact_id, kind)
    @router.post("/{artifact_id}/arm/{stage}")
    async def arm(artifact_id: str, stage: str) -> dict[str, object]:
        return require_controller().arm(artifact_id, stage)
    return router
