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
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image

from doceditor.comment_action_errors import CommentActionError
from doceditor.image_markup import Mark, Region, check_markable, render_marked_image
from infra.database import write_transaction
from infra.passive_changes import append_thread_context_note
from infra.write_effects import after_commit
from registry import models as registry_models
from registry.file_types import image_media_type
from registry.identity_metadata import sha256_file

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


def check_region_shape(*, region: Any, parent_id: str | None, review: bool, quote: str | None,
                       replacement: str | None) -> tuple[float, float, float, float] | None:
    """A region anchors only a new top-level comment, never a reply, review, quote or suggestion."""
    if region is None:
        return None
    if parent_id or review or (quote or "").strip() or replacement is not None:
        raise CommentActionError(
            "region_shape",
            "region anchors a new top-level comment on an image; do not combine it with parent_id, review, "
            "quote or replacement.",
        )
    return parse_region(region)


def is_image_path(path: str | Path | None) -> bool:
    """Whether ``path`` names a file type the viewer shows as an image."""
    return bool(path) and image_media_type(Path(str(path))) is not None


def require_image(path: str | None) -> Path:
    """The artifact's image file, or the error that a region needs one."""
    if not is_image_path(path):
        raise CommentActionError("region_target", "region applies only to an image artifact.")
    if not Path(str(path)).is_file():
        raise CommentActionError("region_target", "the image file is missing, so a region cannot be placed on it.")
    return Path(str(path))


def image_revision(path: Path) -> str:
    """The revision a pin is placed on: the image file's SHA-256."""
    return sha256_file(path)


def move_fields(comment: Mapping[str, Any], fields: dict[str, Any], path: str | None) -> dict[str, Any]:
    """Turn a patch's ``region`` into column updates; a moved pin is placed on the current image."""
    if comment.get("region_number") is None:
        raise CommentActionError("region_target", "only a comment placed on an image region can be moved.")
    x, y, w, h = parse_region(fields["region"])
    rest = {key: value for key, value in fields.items() if key != "region"}
    return {**rest, "region_x": x, "region_y": y, "region_w": w, "region_h": h,
            "image_rev": image_revision(require_image(path))}


def region_of(row: Mapping[str, Any]) -> Region | None:
    """The comment row's region, or None for a text comment or a reply."""
    if row.get("region_number") is None or row.get("region_x") is None:
        return None
    return Region(float(row["region_x"]), float(row["region_y"]),
                  float(row.get("region_w") or 0.0), float(row.get("region_h") or 0.0))


def annotate_stale(threads: list[dict], path: str | None) -> None:
    """Mark each region comment whose image changed since it was placed (FR13.9).

    One hash per call. An image that cannot be read leaves every pin unmarked
    rather than calling them all stale.
    """
    regional = [c for c in threads if c.get("region_number") is not None]
    if not regional:
        return
    try:
        current = image_revision(Path(str(path))) if path else None
    except OSError:
        current = None
    for comment in regional:
        comment["region_stale"] = current is not None and comment.get("image_rev") != current


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
    png = render_marked_image(image.read_bytes(), marks_for(db, rows))
    digest = hashlib.sha256(png).hexdigest()
    target = _working_tray(db, reader_thread_id) / f"{label}-{digest[:16]}.png"
    _save(png, target)
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
    """Give a batch's region comments their wake lines and one marked-up copy (FR13.8).

    The copy shows only this batch's boxes, so its numbers match the lines.
    Delivery holds the SQLite writer, so here only the marks and the image's
    header are read; the copy is drawn once the delivery commits, and a
    rolled-back delivery draws none. It is named for the batch, which delivers
    once. A copy that cannot be made, or a reader tray that cannot be
    resolved, leaves the reason in its place; the text still goes.
    """
    regional = [item for item in items if item["kind"] == "comment" and item.get("region_number") is not None]
    if not regional:
        return
    annotate_stale(regional, path)
    for item in regional:
        item["region_line"] = describe_region(item)
    problem = _unmarkable(path)
    tray = None
    if problem is None:
        try:
            tray = _working_tray(db, reader_thread_id)
        except (ValueError, OSError) as exc:
            problem = f"the reader's working folder could not be used ({exc})."
    if problem is not None:
        line = f"Marked-up image unavailable: {problem}"
    else:
        image = Path(str(path))
        target = tray / f"{image.stem}-feedback-{batch_id}.png"
        marks = marks_for(db, regional)
        after_commit(db, lambda: _draw_copy(image, marks, target, reader_thread_id))
        line = f"Marked-up image (numbered boxes): {target} (open it with read_file to see each region)"
    for item in regional:
        item["marked_image_line"] = line


def _draw_copy(image: Path, marks: list[Mark], target: Path, reader_thread_id: str) -> None:
    """Draw a delivered batch's copy; if it fails, tell the reader the named file will not appear."""
    try:
        _save(render_marked_image(image.read_bytes(), marks), target)
    except RENDER_ERRORS as exc:
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
