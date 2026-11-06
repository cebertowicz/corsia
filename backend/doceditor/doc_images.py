"""Images inside a markdown document, for region comments on them (DCOL-FR13.3).

An image in a document is its markdown, ``![alt](url)``. A region comment on one
is a text comment anchored on exactly that markdown, so it moves with the text
and detaches when the image is removed, like any other anchored comment. This
module finds that markdown around a span and resolves its URL to the workspace
file the editor draws (``resolveImagePath`` in ``frontend/src/lib/cm/markdown-image.ts``):
an absolute path as written, a relative one joined to the document's folder,
never a web or ``data:`` URL.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

from doceditor import diagram_pins
from doceditor.comment_action_errors import CommentActionError, diagram_changed
from doceditor.doc_text import read_doc_text
from doceditor.image_regions import (
    checked_image, comment_image, is_image_path, move_fields, move_fields_on, reader_bases, require_image,
)
from doceditor.quote_anchor import _cp_at_u16, _span16, anchor_context, reanchor


# One inline image: alt text without unescaped brackets, then a destination that
# is either <bracketed> or bare with balanced parentheses, then an optional title.
_IMAGE = re.compile(
    r"!\[(?:[^\[\]\\\n]|\\.)*\]"
    r"\(\s*(?P<url><[^<>\n]*>|(?:[^\s()\\]|\\.|\([^\s()]*\))*)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
_SCHEME = re.compile(r"^[a-z][a-z\d+.-]*:", re.IGNORECASE)
# decodeURIComponent refuses a '%' without two hex digits; unquote would keep it.
_BAD_ESCAPE = re.compile(r"%(?![0-9a-f]{2})", re.IGNORECASE)

NOT_AN_IMAGE = (
    "On a document, a region goes on one of its images: pass quote with that image's markdown "
    "![alt](path), or text inside it such as its path."
)


def image_url(markdown: str) -> str | None:
    """The destination of ``markdown`` when it is exactly one image, else ``None``."""
    match = _IMAGE.fullmatch(markdown)
    return match.group("url") if match else None


def resolve_image_path(doc_path: str | None, url: str) -> str | None:
    """The workspace path ``url`` names from the document at ``doc_path``, or ``None`` for a web URL."""
    target = url.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if _BAD_ESCAPE.search(target):
        return None
    try:
        target = unquote(target, errors="strict")
    except UnicodeDecodeError:
        return None
    if not target or target.startswith("//") or _SCHEME.match(target):
        return None
    if not target.startswith("/"):
        if not doc_path or not doc_path.startswith("/"):
            return None
        target = posixpath.join(posixpath.dirname(doc_path), target)
    # normpath keeps a leading '//'; the editor folds it to one slash.
    return "/" + posixpath.normpath(target).lstrip("/")


def widen_to_image(text: str, from16: int, to16: int) -> tuple[str, int, int, str | None, str | None]:
    """The anchor (quote, from, to, prefix, suffix) on the whole image markdown holding the span.

    An agent names the image by any text inside its markdown (its path, say);
    the comment then anchors on all of it, as a click on the image would.
    """
    start, end = _cp_at_u16(text, from16), _cp_at_u16(text, to16)
    if start is None or end is None:
        raise CommentActionError("region_target", NOT_AN_IMAGE)
    for match in _IMAGE.finditer(text):
        if match.start() <= start and end <= match.end():
            quote = match.group(0)
            image_from, image_to = _span16(text, match.start(), quote)
            prefix, suffix = anchor_context(text, image_from, image_to, quote) or (None, None)
            return quote, image_from, image_to, prefix, suffix
        if match.start() > end:
            break
    raise CommentActionError("region_target", NOT_AN_IMAGE)


def image_file(db, *, doc_path: str | None, markdown: str | None, bases: list[Path]) -> Path:
    """The image file the anchored markdown shows, by the path the editor draws it from.

    It passes the file route's read-scope gate with the reader's ``bases``
    (``image_regions.reader_bases``): an existing file inside a root they cover.
    """
    url = image_url(markdown or "")
    if url is None:
        raise CommentActionError("region_target", NOT_AN_IMAGE)
    path = resolve_image_path(doc_path, url)
    if path is None:
        raise CommentActionError("region_target", "a region goes only on an image stored in the workspace, never a web image.")
    if not is_image_path(path):
        raise CommentActionError("region_target", f"{path} is not an image, so a region cannot be placed on it.")
    checked_image(db, path, bases)
    return Path(path)


def region_patch(db, comment: Mapping[str, Any], fields: dict[str, Any], *, doc_path: str | None,
                 owner_thread_id: str | None, require_diagram_source: bool = False) -> dict[str, Any]:
    """A patch to a comment, checked against the image it is placed on (FR13.5).

    A region's box is in its image's coordinates, so a new anchor must still
    name that image; a moved pin is placed on the image as it is now.
    """
    source = fields.pop("diagram_source", None)
    image_src = comment.get("image_src")
    if not image_src and not is_image_path(doc_path) and comment.get("region_number") is not None:
        return _diagram_patch(comment, fields, doc_path, source, require_diagram_source)
    bases = reader_bases(db, owner_thread_id) if image_src else []
    if image_src and "anchor_text" in fields:
        named = image_file(db, doc_path=doc_path, markdown=fields["anchor_text"], bases=bases)
        _require_same_target(str(named) == image_src, 'image')
    if "region" not in fields:
        return fields
    return move_fields(comment, fields, require_image(comment_image(db, comment, doc_path, bases)))


def _require_same_target(same: bool, kind: str) -> None:
    if not same:
        raise CommentActionError(
            "region_target", f"a comment on part of an {kind} stays on its {kind}; comment on the other {kind} instead."
            if kind == 'image' else
            "a comment on part of a diagram stays on its diagram; comment on the other diagram instead.")


def _diagram_patch(comment: Mapping[str, Any], fields: dict[str, Any], doc_path: str | None,
                   source: str | None, require_source: bool) -> dict[str, Any]:
    """A pin on a diagram has no file: a moved pin is placed on the block as it is now."""
    if 'anchor_text' in fields or 'from_off' in fields or 'to_off' in fields:
        text = read_doc_text(doc_path)
        quote = comment.get('anchor_text') or ''
        original = reanchor(text, quote, from16=comment.get('from_off'), to16=comment.get('to_off'),
                            prefix=comment.get('anchor_prefix'), suffix=comment.get('anchor_suffix'))
        target = (fields.get('from_off', comment.get('from_off')), fields.get('to_off', comment.get('to_off')))
        same = original == target and fields.get('anchor_text', quote) == quote
        _require_same_target(same and diagram_pins.block_at(text, target[0]) is not None, 'diagram')
    if "region" not in fields:
        return fields
    current = diagram_pins.revision(read_doc_text(doc_path), comment.get("from_off"))
    if current is None:
        raise CommentActionError("region_target", "the diagram this pin is on is no longer in the document.")
    if require_source and (source is None or diagram_pins.source_revision(source) != current):
        raise diagram_changed()
    return move_fields_on(comment, fields, current)
