"""Which guidance, at which version, one Turn was dispatched with.

A guidance manifest is the list of delivered guidance sources (system,
project, role, thread and conductor files, the skills index, dynamic rows and
the portable checkpoint) with each one's version, plus the harness, selector
and resolved model it was delivered to. Its id is the hash of that list, so
turns that delivered the same guidance to the same target share one row no
matter how their history differs. Rows hold references and hashes, never
prompt text, and are never deleted.

File versions are stamped once, when a static source is read from disk
(``stamp_raw_versions``), and ride on the source row as ``raw_version``. That
field sits outside the hashed snapshot inventory, and installed rows retain it
through a checkpoint that drops their bytes, so a Turn after an edit still
reports the version it was actually installed with.

Recording never blocks a Turn: every failure lands as a status on the Turn
row (``active_turns.context_manifest_status``, or ``guidance_load_status`` for
a mid-Turn skill read) and the Turn goes on. A Turn is created ``unbound``,
so one whose status write also failed still differs from a row older than
the manifest columns (NULL).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from core.effective_context import ContextSourceEntry

log = logging.getLogger(__name__)

GUIDANCE_ORIGINS = frozenset(
    {"system", "project", "role", "thread", "conductor", "dynamic"}
)
CHECKPOINT_SOURCE_ID = "history:portable-checkpoint"
SKILLS_INDEX_SOURCE_ID = "dynamic:skills-index"
# ``proof`` rows are Native components whose bytes an exact aggregate owns;
# they still carry the per-file version the aggregate was built from.
_DELIVERED = frozenset({"included", "summarized", "proof"})

STATUS_RECORDED = "recorded"
STATUS_NO_SNAPSHOT = "no_snapshot"
STATUS_BUILD_FAILED = "build_failed"
STATUS_WRITE_FAILED = "write_failed"


def _universal_newlines(text: str) -> str:
    """The newline translation ``Path.read_text`` applies to what it returns."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_oid(data: bytes) -> str:
    """The object id ``git hash-object`` gives these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def raw_version_of(path: str, prompt_content: str | None) -> dict[str, Any]:
    """Hash the file behind one delivered row, as it is on disk now."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {"status": "unreadable"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "unreadable"}
    # The row was read a moment ago; a file changed in between is not the one
    # delivered, so it gets no version rather than a wrong one.
    if prompt_content is None or _universal_newlines(text) not in prompt_content:
        return {"status": "changed_since_read"}
    return {
        "status": "read",
        "raw_sha256": _sha256_bytes(data),
        "git_blob_oid": git_blob_oid(data),
        "byte_count": len(data),
    }


def stamp_raw_versions(sources: list[ContextSourceEntry]) -> None:
    """Record the file version of every delivered file-backed static row."""
    for source in sources:
        path = (source.metadata or {}).get("path")
        if (
            isinstance(path, str) and path
            and source.inclusion in {"included", "summarized"}
            and source.prompt_content
        ):
            source.raw_version = raw_version_of(path, source.prompt_content)


def _is_guidance(source: ContextSourceEntry) -> bool:
    if not source.prompt_content or source.inclusion not in _DELIVERED:
        return False
    if source.source_id == CHECKPOINT_SOURCE_ID:
        return True
    return source.origin_section in GUIDANCE_ORIGINS


def _version(source: ContextSourceEntry) -> tuple[str, str | None, dict[str, Any]]:
    """Return (version_kind, reason, raw fields) for one delivered row."""
    raw = source.raw_version
    if raw is not None:
        if raw.get("status") == "read":
            return "git_blob", None, {
                "raw_sha256": raw.get("raw_sha256"),
                "git_blob_oid": raw.get("git_blob_oid"),
            }
        return "unknown", str(raw.get("status") or "unstamped"), {}
    if (source.metadata or {}).get("path"):
        # A file-backed row restored from an install older than this build.
        return "unknown", "retained_before_manifest", {}
    return "generated", None, {}


def _entry(source: ContextSourceEntry, *, model: str | None) -> dict[str, Any]:
    from core.token_estimation import _text_tokens

    tokens = source.to_inventory_dict(model=model).get("prompt_tokens")
    if tokens is None:
        tokens = _text_tokens(source.prompt_content, model) + 4
    kind, reason, raw = _version(source)
    entry = {
        "source_id": source.source_id,
        "origin_section": source.origin_section,
        "stable_reference": source.stable_reference,
        "inclusion": source.inclusion,
        "delivered_sha256": _sha256_bytes(source.prompt_content.encode("utf-8")),
        "prompt_tokens": tokens,
        "raw_sha256": raw.get("raw_sha256"),
        "git_blob_oid": raw.get("git_blob_oid"),
        "version_kind": kind,
        "version_reason": reason,
    }
    path = (source.metadata or {}).get("path")
    if isinstance(path, str) and path and kind != "generated":
        # References identify every source, including generated rows. The path
        # is deliberately narrower: it is the file identity shared by read
        # receipts and correction targets.
        from infra.config import WORKSPACE
        from tools.filesystem_gate import canonical_target_path

        entry["path"] = canonical_target_path(path, workspace=WORKSPACE)
    lessons = (source.metadata or {}).get("lessons")
    if source.source_id == SKILLS_INDEX_SOURCE_ID and isinstance(lessons, list):
        # Offered trial lessons by version; no key means the install predates them.
        entry["lessons"] = [
            {"id": lesson["id"], "behavioral_sha256": lesson["behavioral_sha256"]}
            for lesson in lessons
        ]
    return entry


@dataclass(frozen=True)
class ManifestHeader:
    """The destination one list was delivered to."""

    harness: str
    selector: str | None
    resolved_model: str | None
    resolved_model_source: str | None

    @classmethod
    def from_target(cls, target: Any) -> "ManifestHeader":
        """Read the destination from an ``ExecutionTarget``."""
        harness = str(getattr(target, "harness", "") or "")
        return cls.for_selector(
            harness=harness,
            selector=getattr(target, "catalog_id", None),
            expected_reported_model=getattr(target, "expected_reported_model", None),
            transport_model=getattr(target, "transport_model", None),
        )

    @classmethod
    def for_selector(
        cls, *, harness: str, selector: str | None,
        expected_reported_model: str | None, transport_model: str | None,
    ) -> "ManifestHeader":
        """External harnesses report a catalog identity; Native routes by its
        transport model and has no separately reported one."""
        if expected_reported_model:
            return cls(harness, selector, expected_reported_model, "catalog")
        if harness == "native" and transport_model:
            return cls(harness, selector, transport_model, "transport")
        return cls(harness, selector, None, None)

    def to_dict(self) -> dict[str, Any]:
        """The header fields stored beside the list."""
        return {
            "harness": self.harness,
            "selector": self.selector,
            "resolved_model": self.resolved_model,
            "resolved_model_source": self.resolved_model_source,
        }


@dataclass(frozen=True)
class GuidanceManifest:
    """One content-addressed guidance list."""

    id: str
    header: ManifestHeader
    entries: tuple[dict[str, Any], ...]


def build_guidance_manifest(snapshot: Any, header: ManifestHeader) -> GuidanceManifest:
    """Build the guidance list of one effective-context snapshot."""
    model = (getattr(snapshot, "harness_model_facts", None) or {}).get("model")
    entries = sorted(
        (
            _entry(source, model=model)
            for source in snapshot.source_inventory
            if _is_guidance(source)
        ),
        key=lambda entry: (entry["origin_section"], entry["source_id"],
                           entry["delivered_sha256"]),
    )
    canonical = json.dumps(
        {"header": header.to_dict(), "entries": entries},
        sort_keys=True, separators=(",", ":"),
    )
    return GuidanceManifest(
        id=f"gm-{_sha256_bytes(canonical.encode('utf-8'))}",
        header=header,
        entries=tuple(entries),
    )


def prepared_snapshot(prepared: Any) -> Any | None:
    """Find the effective-context snapshot one prepared dispatch delivers."""
    rendered = getattr(prepared, "rendered", None)
    payload = getattr(rendered, "payload", prepared)
    for carrier in (
        payload,
        # Claude Code: the live frame is what this Turn sends.
        getattr(payload, "frame_reconstruction", None),
        getattr(payload, "seed_reconstruction", None),
        getattr(payload, "reconstruction", None),
    ):
        snapshot = getattr(carrier, "snapshot", None)
        if snapshot is not None and hasattr(snapshot, "source_inventory"):
            return snapshot
    return None


def _write_manifest(db: Any, manifest: GuidanceManifest, now_iso: str) -> None:
    header = manifest.header
    db.execute(
        """INSERT OR IGNORE INTO context_manifests
               (id, harness, selector, resolved_model, resolved_model_source,
                entries_json, entry_count, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            manifest.id, header.harness, header.selector, header.resolved_model,
            header.resolved_model_source,
            json.dumps(list(manifest.entries), sort_keys=True, separators=(",", ":")),
            len(manifest.entries), now_iso,
        ),
    )


def _bind_turn(
    db: Any, thread_id: str, turn_id: str, manifest_id: str | None, status: str,
) -> None:
    from infra.turn_manifest_binding import bind_turn_manifest

    bind_turn_manifest(
        db, thread_id=thread_id, turn_id=turn_id, manifest_id=manifest_id, status=status,
    )


def _mark_turn(thread_id: str, turn_id: str, status: str) -> None:
    from infra.database import write_transaction

    try:
        with write_transaction(None, scope="context_manifest_status") as tx:
            _bind_turn(tx, thread_id, turn_id, None, status)
    except Exception:
        log.exception(
            "guidance_manifest: status %s unrecorded for turn %s", status, turn_id,
        )


def record_turn_manifest(
    thread_id: str, turn_id: str | None, prepared: Any, header: ManifestHeader,
) -> str | None:
    """Record and bind the guidance list one Turn dispatch delivers.

    Returns the list id, or None when it could not be recorded; never raises.
    """
    from infra.database import write_transaction
    from infra.time import utc_now_iso

    if not turn_id:
        return None
    try:
        snapshot = prepared_snapshot(prepared)
    except Exception:
        log.exception("guidance_manifest: snapshot lookup failed for %s", turn_id)
        snapshot = None
    if snapshot is None:
        log.warning("guidance_manifest: no snapshot on dispatch for turn %s", turn_id)
        _mark_turn(thread_id, turn_id, STATUS_NO_SNAPSHOT)
        return None
    try:
        manifest = build_guidance_manifest(snapshot, header)
    except Exception:
        log.exception("guidance_manifest: build failed for turn %s", turn_id)
        _mark_turn(thread_id, turn_id, STATUS_BUILD_FAILED)
        return None
    try:
        with write_transaction(None, scope="record_turn_manifest") as tx:
            _write_manifest(tx, manifest, utc_now_iso())
            _bind_turn(tx, thread_id, turn_id, manifest.id, STATUS_RECORDED)
    except Exception:
        log.exception("guidance_manifest: write failed for turn %s", turn_id)
        _mark_turn(thread_id, turn_id, STATUS_WRITE_FAILED)
        return None
    return manifest.id


def record_dispatch_manifest(thread_id: str, turn_id: str | None, prepared: Any) -> str | None:
    """Record the list one send dispatch (an ``infra.input_render.PreparedBatch``) delivers."""
    return record_turn_manifest(
        thread_id, turn_id, prepared.prepared_context,
        ManifestHeader.from_target(prepared.target),
    )


def record_continuation_manifest(
    thread_id: str, turn_id: str | None, payload: Any, *, selector: str, transport_model: str,
) -> str | None:
    """Rebind a Native Turn to the list a mid-Turn checkpoint continuation delivers."""
    return record_turn_manifest(
        thread_id, turn_id, payload,
        ManifestHeader.for_selector(
            harness="native", selector=selector,
            expected_reported_model=None, transport_model=transport_model,
        ),
    )


def record_guidance_load(
    *, thread_id: str | None, turn_id: str | None, path: Path, data: bytes,
    returned_text: str, partial: bool,
) -> None:
    """Record one mid-Turn read of a ``SKILL.md``; never raises.

    ``data`` is the bytes the read returned text from, never a second read.
    """
    from infra.database import write_transaction
    from infra.time import utc_now_iso
    import uuid

    try:
        with write_transaction(None, scope="record_guidance_load") as tx:
            tx.execute(
                """INSERT INTO guidance_loads
                       (id, thread_id, turn_id, path, raw_sha256, git_blob_oid,
                        delivered_sha256, byte_count, partial, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex, thread_id, turn_id, str(path),
                    _sha256_bytes(data), git_blob_oid(data),
                    _sha256_bytes(returned_text.encode("utf-8")), len(data),
                    int(partial), utc_now_iso(),
                ),
            )
    except Exception:
        log.exception("guidance_manifest: skill read of %s unrecorded", path)
        if thread_id and turn_id:
            _mark_guidance_load_failed(thread_id, turn_id)


def _mark_guidance_load_failed(thread_id: str, turn_id: str) -> None:
    from infra.database import write_transaction
    from infra.turn_manifest_binding import mark_guidance_load_status

    try:
        with write_transaction(None, scope="guidance_load_status") as tx:
            mark_guidance_load_status(
                tx, thread_id=thread_id, turn_id=turn_id, status=STATUS_WRITE_FAILED,
            )
    except Exception:
        log.exception(
            "guidance_manifest: skill read failure unrecorded for turn %s", turn_id,
        )
