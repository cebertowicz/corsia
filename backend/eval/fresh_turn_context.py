"""Temporary, audited context attachment for one hidden fresh eval turn."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FencedTarget:
    """Immutable target bytes that must be delivered to an eval turn."""

    path: str
    commit: str
    blob: bytes
    role: str

    @property
    def blob_sha(self) -> str:
        return hashlib.sha256(self.blob).hexdigest()


@dataclass(frozen=True)
class EvalContextReceipt:
    """Attachments and fixture facts retained until the C1 outcome is recorded."""

    thread_id: str
    user_id: str
    fixture: Path
    fixture_sha: str
    neighbour_shas: tuple[str, ...]


def attach_fenced_context(
    thread_id: str,
    user_id: str,
    *,
    owner_thread_id: str,
    project_id: str | None,
    target: FencedTarget,
) -> EvalContextReceipt:
    """Attach owner neighbours and replace the target entry with a fenced fixture."""
    from api.context_bootloader import BootloaderTarget, _write_context_change
    from core.bootloader_management import normalize_bootloader_entries, thread_bootloader_files
    from core.dynamic_context_registry import _thread_context_dir_entries
    from core.workspace_layout import resolve_project_root, resolve_thread_root, resolve_workspace_root
    from infra import config
    from infra.database import get_db

    workspace = Path(config.WORKSPACE)
    db = get_db()
    try:
        scope_root = (resolve_project_root(workspace, project_id, db=db) if project_id else resolve_workspace_root(workspace))
        if scope_root is None:
            raise ValueError("eval project scope is unavailable")
        owner_entries = thread_bootloader_files(owner_thread_id, db=db)
        seen = {Path(entry).name for entry in owner_entries}
        for entry in _thread_context_dir_entries(owner_thread_id, project_id, workspace, db):
            if Path(entry).name not in seen:
                owner_entries.append(entry)
                seen.add(Path(entry).name)
        target_path = (workspace / target.path).resolve(strict=False)
        resolved = [Path(entry).expanduser().resolve(strict=False) for entry in owner_entries]
        indexes = [index for index, entry in enumerate(resolved) if entry == target_path]
        if len(indexes) != 1:
            raise ValueError("carry-in target is not exactly one owner thread bootloader entry")
        context_root = resolve_thread_root(scope_root, thread_id) / "context"
        context_root.mkdir(parents=True, exist_ok=True)
        fixture = context_root / "CARRY_IN_TARGET.md"
        try:
            fixture.write_bytes(target.blob)
            fixture_sha = hashlib.sha256(fixture.read_bytes()).hexdigest()
            if fixture_sha != target.blob_sha:
                raise ValueError("carry-in fixture does not match the recorded target blob")
            resolved[indexes[0]] = fixture.resolve()
            entries = [str(entry) for entry in resolved]
            roots = tuple(dict.fromkeys([context_root, *(entry.parent for entry in resolved)]))
            bootloader = BootloaderTarget("thread", thread_id, context_root, roots, True)
            files = normalize_bootloader_entries(entries, bootloader)
            _write_context_change(
                db,
                target=bootloader,
                user_id=user_id,
                action="attach_eval_fenced_context",
                files=files,
                expected_files=[],
            )
            neighbour_shas = tuple(
                hashlib.sha256(entry.read_bytes()).hexdigest()
                for entry in resolved
                if entry != fixture and entry.is_file()
            )
            return EvalContextReceipt(thread_id, user_id, fixture, fixture_sha, neighbour_shas)
        except Exception:
            fixture.unlink(missing_ok=True)
            raise
    finally:
        db.close()


async def detach_and_delete(receipt: EvalContextReceipt) -> None:
    """Remove temporary attachment and fixture, then delete the hidden thread."""
    from api.context_bootloader import BootloaderTarget, _write_context_change
    from api.thread_deletion import delete_owned_thread
    from core.bootloader_management import thread_bootloader_files
    from infra.database import get_db

    db = get_db()
    try:
        files = thread_bootloader_files(receipt.thread_id, db=db)
        target = BootloaderTarget("thread", receipt.thread_id, receipt.fixture.parent, (receipt.fixture.parent,), True)
        _write_context_change(
            db,
            target=target,
            user_id=receipt.user_id,
            action="detach_eval_fenced_context",
            files=[],
            expected_files=files,
        )
    finally:
        db.close()
    receipt.fixture.unlink(missing_ok=True)
    status = await delete_owned_thread(receipt.thread_id, receipt.user_id, [])
    if status != "ok":
        raise RuntimeError(f"could not delete hidden eval thread: {status}")
