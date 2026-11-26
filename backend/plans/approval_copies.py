"""Prepare immutable copies of an approved document and its pictures before a write."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

from doceditor.doc_images import resolve_image_path
from doceditor.document_scope import path_write_allowed, scope_roots


copy_lock = threading.Lock()


@dataclass
class PreparedCopies:
    doc_sha256: str
    images: list[dict]
    created: list[Path]
    contents: dict[Path, bytes]

    def restore_missing(self) -> None:
        """Restore copies reused before a competing cleanup committed."""
        with copy_lock:
            for path, raw in self.contents.items():
                if not path.exists():
                    _copy(path.parent, raw, path.suffix, [], {})

    def discard(self) -> None:
        """Remove copies created by a write that rolled back."""
        from infra.database import get_db

        db = get_db()
        try:
            _discard_created(self.created, db)
        finally:
            db.close()


def _discard_created(paths: list[Path], db) -> None:
    """Retain a newly created copy if another committed approval now shares it."""
    from plans.approval_records import _delete_unreferenced

    copies = []
    for path in paths:
        try:
            copies.append({"copy_path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        except FileNotFoundError:
            continue
    _delete_unreferenced(db, copies)


def _copy(root: Path, raw: bytes, suffix: str, created: list[Path], contents: dict[Path, bytes]) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest[:16] + suffix)
    try:
        with path.open("xb") as dest:
            created.append(path)
            dest.write(raw)
        path.chmod(0o444)
    except FileExistsError:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("approved version damaged")
    contents[path] = raw
    return digest


def _images(text: str) -> list[str]:
    parser = MarkdownIt("commonmark").enable("table")
    urls: list[str] = []
    for block in parser.parse(text):
        for child in block.children or []:
            if child.type == "image":
                urls.append(str(child.attrGet("src") or ""))
    return urls


def prepare_bar(artifact_id: str, expected_sha256: str | None, *, db) -> PreparedCopies | None:
    """Prepare a document approval from its server snapshot and the bar's hash."""
    from doceditor.live_tools import find_live
    from plans.approved_versions import _project_id

    row = db.execute("SELECT type,path,external_id FROM artifact_identities WHERE id=?", (artifact_id,)).fetchone()
    if row is None or row["type"] != "document":
        return None
    from doceditor.review_bar import AnswerRefused

    if expected_sha256 is None:
        raise AnswerRefused("doc_hash_required", "doc_sha256 is required to approve a document.")
    path = Path(row["path"] or row["external_id"])
    entry = find_live(path)
    raw = entry.live.snapshot[0].encode("utf-8") if entry is not None else path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise AnswerRefused("ask_stale", "The document is still changing. Try again in a moment.",
                            cause="document_changed")
    return prepare(path, raw, _project_id(artifact_id, db), db=db)


def prepare_save(path: Path, raw: bytes, *, db) -> PreparedCopies | None:
    """Prepare a new version only when this document already has an approval."""
    from registry import service
    from plans.approved_versions import _project_id

    artifact = service.resolve_file_identity(str(path), db=db)
    if artifact is None or db.execute(
        "SELECT 1 FROM artifact_approvals WHERE artifact_id=? LIMIT 1", (artifact["id"],)
    ).fetchone() is None:
        return None
    return prepare(path, raw, _project_id(artifact["id"], db), db=db)


def prepare(path: Path, raw: bytes, project_id: str | None, *, db) -> PreparedCopies:
    """Copy server bytes and images; a missing or inaccessible image is recorded."""
    _, roots = scope_roots(project_id, db)
    if not roots:
        raise ValueError("document scope has no planning folder")
    target = roots[-1] / ".corsia" / "approved"
    created: list[Path] = []
    contents: dict[Path, bytes] = {}
    try:
        digest = _copy(target, raw, path.suffix, created, contents)
        images: list[dict] = []
        for url in _images(raw.decode("utf-8")):
            resolved = resolve_image_path(str(path), url)
            original = str(Path(resolved).resolve()) if resolved else url
            try:
                if resolved is None:
                    raise ValueError("remote or invalid image address")
                image = Path(resolved).resolve()
                if not path_write_allowed(image, roots):
                    raise ValueError("image outside document scope")
                sha = _copy(target, image.read_bytes(), image.suffix, created, contents)
                images.append({"path": original, "sha256": sha})
            except (OSError, ValueError) as exc:
                images.append({"path": original, "not_kept": str(exc)})
        return PreparedCopies(digest, images, created, contents)
    except Exception:
        _discard_created(created, db)
        raise
