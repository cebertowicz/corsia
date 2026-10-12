"""Region comments on images: shape checks, revisions, colours and marked-up copies.

A region comment is an ordinary document comment that also carries a box or
point in fractions of its image (DCOL-FR13.3). This module holds what every
adapter shares: the region check, the image revision a pin is placed on
(FR13.9), the author's colour (FR13.5), and the marked-up PNG an agent gets back
from ``doc_comment_create`` (FR13.7) and a delivered feedback batch names
(FR13.8). Marked-up copies go to the reading thread's ``working`` tray, which
expires on its own; the user's image is never written.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image

from doceditor import live_events
from doceditor.comment_action_errors import CommentActionError
from doceditor.document_scope import ProjectNotFound, scope_bases
from doceditor.image_markup import Mark, Region, check_markable, render_marked_image
from infra.database import write_transaction
from infra.passive_changes import append_thread_context_note
from infra.write_effects import after_commit
from registry import models as registry_models
from registry.file_types import image_media_type
from registry.identity_metadata import sha256_file
from registry.path_access import ReadRefused, checked_read

# Mirrors the viewer's AGENT_COLORS and agentColor() (frontend/src/lib/docPresence.ts),
# so a tag is the same colour in the picture an agent reads and on screen.
AGENT_COLORS = ("#8b5cf6", "#2dd4bf", "#60a5fa", "#f59e0b", "#e0489e", "#22c55e", "#ef4444", "#06b6d4")
# Mirrors projectColor() (frontend/src/lib/utils/projectColors.ts) for a project with no accent.
_PROJECT_HUES = (210, 155, 38, 280, 340, 175, 60, 310, 20, 245)
_NO_PROJECT_COLOR = "hsl(260, 70%, 62%)"

_EPSILON = 1e-6

# What a marked-up copy can fail with: a missing or unreadable file, a format
# Pillow cannot decode, or an image past the decode budget.
RENDER_ERRORS = (OSError, ValueError, Image.DecompressionBombError)
_REFUSED = {"missing": "could not be found", "outside": "is outside this document's scope", "not_file": "is not a file"}


def parse_region(value: Any) -> tuple[float, float, float, float]:
    """Return a checked (x, y, w, h) in fractions of the image; w = h = 0 is a point."""
    if not isinstance(value, Mapping):
        raise CommentActionError("region_shape", "region must be an object with x and y (and w, h for a box).")
    try:
        x, y = float(value["x"]), float(value["y"])
        w, h = float(value.get("w") or 0.0), float(value.get("h") or 0.0)
    except (KeyError, TypeError, ValueError) as exc:
        raise CommentActionError(
            "region_shape", "region needs numeric x and y (and w, h for a box), as fractions 0..1 of the image."
        ) from exc
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise CommentActionError("region_bounds", "region x and y must be fractions from 0 to 1 of the image.")
    if w < 0 or h < 0 or (w == 0) != (h == 0):
        raise CommentActionError(
            "region_bounds", "region w and h must both be positive for a box, or both 0 (or omitted) for a point."
        )
    if x + w > 1 + _EPSILON or y + h > 1 + _EPSILON:
        raise CommentActionError("region_bounds", "region box must stay inside the image (x + w <= 1, y + h <= 1).")
    return x, y, min(w, 1 - x), min(h, 1 - y)


def check_region_shape(*, region: Any, parent_id: str | None, review: bool,
                       replacement: str | None) -> tuple[float, float, float, float] | None:
    """A region anchors only a new top-level comment, never a reply, review or suggestion.

    Whether it also takes a quote depends on the target (``check_region_target``).
    """
    if region is None:
        return None
    if parent_id or review or replacement is not None:
        raise CommentActionError(
            "region_shape",
            "region anchors a new top-level comment on an image; do not combine it with parent_id, review "
            "or replacement.",
        )
    return parse_region(region)


def check_region_target(path: str | None, quote: str | None) -> None:
    """An image artifact takes a region alone; a document takes it with a quote naming one of its images."""
    quoted = bool((quote or "").strip())
    if is_image_path(path):
        if quoted:
            raise CommentActionError(
                "region_shape", "on an image artifact, region places the comment by itself; do not pass quote.",
            )
    elif not quoted:
        raise CommentActionError(
            "region_target",
            "on a document, region needs the image it is on: pass quote with that image's markdown "
            "![alt](path), or text inside it such as its path.",
        )


def is_image_path(path: str | Path | None) -> bool:
    """Whether ``path`` names a file type the viewer shows as an image."""
    return bool(path) and image_media_type(Path(str(path))) is not None


def image_of(row: Mapping[str, Any], path: str | None) -> str | None:
    """The image a region comment is on: the one in its document it names, else its artifact's file."""
    return row.get("image_src") or path


def require_image(path: str | None) -> Path:
    """The image file, or the error that a region needs one."""
    if not is_image_path(path):
        raise CommentActionError("region_target", "region applies only to an image.")
    if not Path(str(path)).is_file():
        raise CommentActionError("region_target", "the image file is missing, so a region cannot be placed on it.")
    return Path(str(path))


def reader_bases(db, thread_id: str | None) -> list[Path]:
    """The roots ``thread_id`` reads files from, scoped as the file route scopes them; resolved once per request.

    A thread in a project reads that project's roots; a thread outside any project, the workspace's.
    """
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone() if thread_id else None
    try:
        return scope_bases(row["project_id"] if row else None, db)
    except ProjectNotFound:
        return []


def checked_image(db, path: str, bases: list[Path]) -> Path:
    """The file at ``path`` as the read-scope gate passes it for ``bases``, or the region error."""
    try:
        return checked_read(path, bases, db=db)
    except ReadRefused as exc:
        raise CommentActionError("region_target", f"the image {path} {_REFUSED[exc.reason]}.") from exc


def comment_image(db, row: Mapping[str, Any], path: str | None, bases: list[Path]) -> Path | None:
    """The image a region comment is on, or ``None`` when its artifact has none. An artifact is its own
    file; a document's image is a stored path, not a grant, so each read takes it through the gate again."""
    if row.get("image_src"):
        return checked_image(db, row["image_src"], bases)
    return Path(path) if path else None


def _bases_for(db, groups: Iterable[list[dict]], thread_id: str | None) -> list[Path]:
    """The reader's roots, resolved only when some image in ``groups`` sits inside a document."""
    return reader_bases(db, thread_id) if any(group[0].get("image_src") for group in groups) else []


def image_revision(path: Path) -> str:
    """The revision a pin is placed on: the image file's SHA-256, hashed once per version of the file.

    Every comment read judges each commented image's staleness (FR13.9), so a
    file whose identity, size and times are unchanged keeps its last hash: a
    stat per image per read, not a full read.
    """
    st = path.stat()
    return _file_sha256(str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@lru_cache(maxsize=512)
def _file_sha256(path: str, *version: int) -> str:
    return sha256_file(Path(path))


def move_fields(comment: Mapping[str, Any], fields: dict[str, Any], image: Path) -> dict[str, Any]:
    """Turn a patch's ``region`` into column updates; a moved pin is placed on ``image`` as it is now."""
    if comment.get("region_number") is None:
        raise CommentActionError("region_target", "only a comment placed on an image region can be moved.")
    x, y, w, h = parse_region(fields["region"])
    rest = {key: value for key, value in fields.items() if key != "region"}
    return {**rest, "region_x": x, "region_y": y, "region_w": w, "region_h": h, "image_rev": image_revision(image)}


def region_of(row: Mapping[str, Any]) -> Region | None:
    """The comment row's region, or None for a text comment or a reply."""
    if row.get("region_number") is None or row.get("region_x") is None:
        return None
    return Region(float(row["region_x"]), float(row["region_y"]),
                  float(row.get("region_w") or 0.0), float(row.get("region_h") or 0.0))


def _by_image(rows: Iterable[dict], path: str | None) -> dict[str | None, list[dict]]:
    """The region comments among ``rows``, grouped by the image each is on."""
    groups: dict[str | None, list[dict]] = {}
    for row in rows:
        if row.get("region_number") is not None:
            groups.setdefault(image_of(row, path), []).append(row)
    return groups


def _mark_stale(group: list[dict], image: Path | None) -> None:
    """An image that cannot be read leaves its pins unmarked rather than calling them all stale."""
    try:
        revision = image_revision(image) if image else None
    except OSError:
        revision = None
    for row in group:
        row["region_stale"] = revision is not None and row.get("image_rev") != revision


def annotate_stale(db, threads: list[dict], path: str | None, reader_thread_id: str | None) -> None:
    """Mark each region comment whose image changed since it was placed (FR13.9), reading each image once."""
    groups = list(_by_image(threads, path).values())
    bases = _bases_for(db, groups, reader_thread_id)
    for group in groups:
        try:
            image = comment_image(db, group[0], path, bases)
        except CommentActionError:
            image = None
        _mark_stale(group, image)


def agent_color(thread_id: str) -> str:
    """The viewer's agentColor(): a 32-bit string hash over the thread id."""
    value = 0
    for char in thread_id:
        value = ((value << 5) - value + ord(char)) & 0xFFFFFFFF
    signed = value - (1 << 32) if value & 0x80000000 else value
    return AGENT_COLORS[abs(signed) % len(AGENT_COLORS)]


def _project_color(project_id: str | None, accent: str | None) -> str:
    if accent:
        return accent
    if not project_id:
        return _NO_PROJECT_COLOR
    index = sum(ord(ch) * (i + 1) for i, ch in enumerate(project_id)) % len(_PROJECT_HUES)
    return f"hsl({_PROJECT_HUES[index]}, 70%, 62%)"


def author_color(db, row: Mapping[str, Any]) -> str:
    """An agent's own colour; a person's is the accent of the image owner's project."""
    if row.get("author_kind") == "agent" and row.get("author_thread_id"):
        return agent_color(str(row["author_thread_id"]))
    # The owner thread lives on the artifact's registration, which the registry resolves.
    artifact = registry_models.get_artifact(str(row["artifact_id"]), db=db)
    owner = artifact.get("parent_thread_id") if artifact else None
    found = db.execute(
        """SELECT p.id, p.accent_color FROM threads t
           JOIN projects p ON p.id=t.project_id WHERE t.id=?""",
        (owner,),
    ).fetchone() if owner else None
    return _project_color(found["id"], found["accent_color"]) if found else _NO_PROJECT_COLOR


def marks_for(db, rows: Iterable[Mapping[str, Any]]) -> list[Mark]:
    """Numbered, author-coloured marks for the rows that carry a region, by number."""
    marks = []
    colors: dict[tuple[str, str], str] = {}  # a person's colour costs two reads per artifact
    for row in rows:
        region = region_of(row)
        if region is not None:
            agent = row.get("author_kind") == "agent" and row.get("author_thread_id")
            key = ("agent", str(row["author_thread_id"])) if agent else ("artifact", str(row["artifact_id"]))
            if key not in colors:
                colors[key] = author_color(db, row)
            marks.append(Mark(int(row["region_number"]), region, colors[key]))
    return sorted(marks, key=lambda mark: mark.number)


def _working_tray(db, thread_id: str) -> Path:
    from core.workspace_layout import resolve_project_root, resolve_thread_working_root, resolve_workspace_root
    from infra import config

    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    project_id = row["project_id"] if row else None
    scope = resolve_project_root(config.WORKSPACE, project_id, db=db) if project_id else None
    return resolve_thread_working_root(scope or resolve_workspace_root(config.WORKSPACE), thread_id)


@dataclass(frozen=True)
class MarkedImage:
    path: Path
    sha256: str
    byte_size: int


def write_marked_image(db, *, image: Path, rows: Iterable[Mapping[str, Any]], reader_thread_id: str,
                       label: str) -> MarkedImage:
    """Render ``rows``' numbered regions onto ``image`` into the reader's working tray.

    The name carries the content hash, so a copy is never overwritten with other
    bytes: a tool result that names an earlier copy still verifies.
    """
    try:
        png = render_marked_image(image.read_bytes(), marks_for(db, rows))
        digest = hashlib.sha256(png).hexdigest()
        target = _working_tray(db, reader_thread_id) / f"{label}-{digest[:16]}.png"
        _save(png, target)
    except RENDER_ERRORS as exc:
        live_events.marked_copy_failed("agent_result", exc)
        raise
    return MarkedImage(target, digest, len(png))


def _save(png: bytes, target: Path) -> None:
    """Write ``png`` to ``target`` whole, keeping a copy that is already there."""
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".png.partial")
    partial.write_bytes(png)
    partial.replace(target)


def annotate_delivery(db, items: list[dict], *, path: str | None, reader_thread_id: str, batch_id: str) -> None:
    """Give a batch's region comments their wake lines and one marked-up copy per image (FR13.8).

    A copy shows only this batch's boxes on its image, so its numbers match the
    lines. Delivery holds the SQLite writer, so here only the marks and the
    image's header are read; the copy is drawn once the delivery commits, and a
    rolled-back delivery draws none. It is named for the batch, which delivers
    once. A copy that cannot be made, or a reader tray that cannot be
    resolved, leaves the reason in its place; the text still goes.
    """
    by_image = _by_image((item for item in items if item["kind"] == "comment"), path)
    if not by_image:
        return
    tray, tray_problem = None, None
    try:
        tray = _working_tray(db, reader_thread_id)
    except (ValueError, OSError) as exc:
        tray_problem = f"the reader's working folder could not be used ({exc})."
    bases = _bases_for(db, by_image.values(), reader_thread_id)
    for image_path, group in by_image.items():
        try:
            image = comment_image(db, group[0], path, bases)
            problem = _unmarkable(str(image) if image else None)
        except CommentActionError as exc:
            image, problem = None, exc.message
        _mark_stale(group, image)
        problem = problem or tray_problem
        if problem is not None or tray is None or image is None:
            line = f"Marked-up image unavailable: {problem}"
        else:
            # Images in one document can share a file name, so each one's copy also carries its path's hash.
            tag = "" if image_path == path else f"-{hashlib.sha256(str(image_path).encode()).hexdigest()[:8]}"
            target = tray / f"{Path(str(image_path)).stem}-feedback-{batch_id}{tag}.png"
            after_commit(db, partial(_draw_copy, image, marks_for(db, group), target, reader_thread_id))
            line = f"Marked-up image (numbered boxes): {target} (open it with read_file to see each region)"
        for item in group:
            item["region_line"] = describe_region(item)
            item["marked_image_line"] = line


def _draw_copy(image: Path, marks: list[Mark], target: Path, reader_thread_id: str) -> None:
    """Draw a delivered batch's copy; if it fails, tell the reader the named file will not appear."""
    try:
        _save(render_marked_image(image.read_bytes(), marks), target)
    except RENDER_ERRORS as exc:
        live_events.marked_copy_failed("delivery", exc)
        with write_transaction(None, scope="marked_copy_failed") as connection:
            append_thread_context_note(
                connection,
                thread_id=reader_thread_id,
                summary=(f"The marked-up image {target.name} named in your feedback task could not be "
                         f"drawn ({exc}); the region lines in the task still say where each comment is."),
                details={"path": str(target)},
            )


def _unmarkable(path: str | None) -> str | None:
    """Why no marked-up copy of the image at ``path`` can be drawn, or ``None`` when one can."""
    if not path:
        return "the artifact has no image file."
    try:
        check_markable(Path(path))
    except (ValueError, Image.DecompressionBombError) as exc:
        return str(exc)
    except OSError:
        return "the image file could not be read."
    return None


def describe_region(row: Mapping[str, Any]) -> str | None:
    """'Region 3: box at x 10%, y 20%, 30% wide, 10% tall' for the wake text."""
    region = region_of(row)
    if region is None:
        return None

    def pct(value: float) -> str:
        """A fraction as a percentage with at most one decimal."""
        return f"{round(value * 100, 1):g}%"

    where = f"x {pct(region.x)}, y {pct(region.y)}"
    if region.is_point:
        text = f"Region {row['region_number']}: point at {where}"
    else:
        text = f"Region {row['region_number']}: box at {where}, {pct(region.w)} wide, {pct(region.h)} tall"
    if row.get("region_stale"):
        text += " (the image changed after this was placed)"
    return text
