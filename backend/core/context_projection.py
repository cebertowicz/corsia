"""The one place a Turn may see a file at a recorded revision instead of on disk.

Every thread sees the live workspace except a C1 carry-in eval thread
(``eval-c1-<identity>-<side><n>``, opened by ``infra.carry_in_drain``). That
thread sees its case's target exactly as committed at the revision its side
tests (``core.carry_in_facts.side_bytes``) and every other path live.
Context-source collection (``core.dynamic_context_registry._static_sources``)
and the read-only file tools (``read_file``, ``grep``) ask ``thread_projection``
for the thread they serve, so the target can reach the model by neither route
at its live bytes. A C1 thread whose projection cannot be proven raises
``ProjectionUnavailable``, which fails its Turn before the provider call.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.effective_context import ContextSourceEntry

C1_THREAD_PREFIX = "eval-c1-"
_DELIVERED = frozenset({"included", "summarized"})


class ProjectionUnavailable(RuntimeError):
    """A C1 eval thread's target revision cannot be proven; the Turn must not dispatch."""


def c1_thread_id(identity: str, label: str) -> str:
    """The eval thread for one sample: its case identity and side label (``b1``, ``c2``)."""
    return f"{C1_THREAD_PREFIX}{identity}-{label}"


def _c1_sample(thread_id: str | None) -> tuple[str, str] | None:
    if not thread_id or not thread_id.startswith(C1_THREAD_PREFIX):
        return None
    identity, _sep, label = thread_id[len(C1_THREAD_PREFIX):].rpartition("-")
    return (identity, label[:1]) if identity and label else ("", "")


@dataclass(frozen=True)
class TargetProjection:
    """One target path and the exact bytes a thread must see for it; None means absent."""

    path: Path
    data: bytes | None

    def covers(self, path: Path) -> bool:
        return Path(path).resolve(strict=False) == self.path

    def within(self, directory: Path) -> bool:
        return self.covers(directory) or self.path.is_relative_to(Path(directory).resolve(strict=False))

    @property
    def text(self) -> str | None:
        return None if self.data is None else self.data.decode("utf-8")

    def raw_version(self) -> dict | None:
        if self.data is None:
            return None
        from core.guidance_manifest import git_blob_oid

        return {
            "status": "read", "raw_sha256": hashlib.sha256(self.data).hexdigest(),
            "git_blob_oid": git_blob_oid(self.data), "byte_count": len(self.data),
        }


def thread_projection(thread_id: str | None, *, workspace: Path, db=None) -> TargetProjection | None:
    """None for an ordinary thread; the proven target revision for a C1 eval thread.

    Raises ``ProjectionUnavailable`` when a C1 thread's case is unknown, not
    claimed, already settled, or its revision unreadable.
    """
    sample = _c1_sample(thread_id)
    if sample is None:
        return None
    from core.carry_in_facts import SIDES, claimed_fact, side_bytes

    identity, side = sample
    if side not in SIDES:
        raise ProjectionUnavailable(f"{thread_id} names no carry-in side")
    own = db is None
    if own:
        from infra.database import get_db

        db = get_db()
    try:
        fact = claimed_fact(db, identity)
        data = side_bytes(workspace, fact, side)
        if data is not None:
            data.decode("utf-8")
    except (LookupError, RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        raise ProjectionUnavailable(f"{thread_id}: {exc}") from exc
    finally:
        if own:
            db.close()
    return TargetProjection((Path(workspace) / fact.target_path).resolve(strict=False), data)


async def tool_projection(thread_id: str | None, workspace: Path | None) -> TargetProjection | None:
    """``thread_projection`` for a read-only file tool; ordinary threads skip all DB and Git work."""
    if _c1_sample(thread_id) is None:
        return None
    from infra.config import WORKSPACE
    from infra.db_async import run_in_db

    return await run_in_db(
        lambda: thread_projection(thread_id, workspace=workspace or WORKSPACE),
    )


def project_sources(sources: list[ContextSourceEntry], projection: TargetProjection | None) -> None:
    """Put the projected revision in place of the live target in every delivered row.

    A row is file-backed when its metadata names a ``path``; it delivers that
    file's text verbatim, as ``guidance_manifest.raw_version_of`` relies on.
    A row the projection cannot rewrite exactly fails closed.
    """
    if projection is None:
        return
    from core.guidance_manifest import _universal_newlines

    for source in sources:
        path = (source.metadata or {}).get("path")
        if not isinstance(path, str) or not path or not projection.covers(Path(path)):
            continue
        if source.inclusion not in _DELIVERED or not source.prompt_content:
            if projection.data is not None:
                raise ProjectionUnavailable(f"{path} is not delivered live, so its revision cannot be")
            continue
        try:
            live = _universal_newlines(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjectionUnavailable(f"{path} unreadable while projecting: {exc}") from exc
        if not live or live not in source.prompt_content:
            raise ProjectionUnavailable(f"{path} is not delivered verbatim, so its revision cannot be")
        if projection.text is None:
            source.inclusion, source.freshness = "omitted", "missing"
            source.reason = "absent at the tested revision"
            source.prompt_content, source.raw_version = None, None
            continue
        source.prompt_content = source.prompt_content.replace(
            live, _universal_newlines(projection.text), 1,
        )
        source.raw_version = projection.raw_version()
