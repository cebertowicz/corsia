"""Typed logical references to content inside the selected workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes, urlsplit

from infra.platform_paths import WorkspaceSelection

_SCHEME = "workspace"
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9._~-]+$")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WorkspaceReference:
    """A stable workspace identity plus a decoded relative POSIX path."""

    workspace_id: str
    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate direct construction through the codec invariants."""
        if not isinstance(self.workspace_id, str) or not _WORKSPACE_ID.fullmatch(
            self.workspace_id
        ):
            raise ValueError("workspace id is missing or not URI-safe")
        normalized = tuple(self.parts)
        _validate_parts(normalized)
        object.__setattr__(self, "parts", normalized)

    @classmethod
    def parse(cls, raw: str) -> "WorkspaceReference":
        """Parse a canonical ``workspace://<id>/<relative-path>`` reference."""
        if not isinstance(raw, str) or not raw.startswith(f"{_SCHEME}://"):
            raise ValueError("expected a workspace:// logical reference")
        parsed = urlsplit(raw)
        if parsed.scheme != _SCHEME or not _WORKSPACE_ID.fullmatch(parsed.netloc):
            raise ValueError("workspace id is missing or invalid")
        if parsed.query or parsed.fragment:
            raise ValueError("workspace reference cannot contain query or fragment")
        if not parsed.path.startswith("/") or not parsed.path:
            raise ValueError("workspace reference must include an absolute URI path")
        if "\\" in parsed.path or _ENCODED_SEPARATOR.search(parsed.path):
            raise ValueError("workspace reference contains an encoded or raw separator")

        encoded_relative = parsed.path[1:]
        if not encoded_relative:
            return cls(parsed.netloc)
        encoded_parts = encoded_relative.split("/")
        if any(not part for part in encoded_parts):
            raise ValueError("workspace reference contains an empty path segment")

        decoded_parts = tuple(_decode_part(part) for part in encoded_parts)
        reference = cls(parsed.netloc, decoded_parts)
        if reference.serialize() != raw:
            raise ValueError("workspace reference is not in canonical encoded form")
        return reference

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        selection: WorkspaceSelection,
    ) -> "WorkspaceReference":
        """Create a logical reference for a path contained by ``selection``."""
        root = selection.root.resolve(strict=False)
        target = Path(path).expanduser().resolve(strict=False)
        if target != root and not target.is_relative_to(root):
            raise ValueError(f"path is outside selected workspace: {target}")
        return cls(selection.workspace_id, tuple(target.relative_to(root).parts))

    def serialize(self) -> str:
        """Return the canonical percent-encoded logical form."""
        suffix = "/".join(quote(part, safe="-._~") for part in self.parts)
        return f"{_SCHEME}://{self.workspace_id}/{suffix}"

    def resolve(self, selection: WorkspaceSelection) -> Path:
        """Resolve under the matching selected root, rejecting symlink escape."""
        if self.workspace_id != selection.workspace_id:
            raise ValueError(
                "workspace id does not match the selected workspace: "
                f"{self.workspace_id!r} != {selection.workspace_id!r}"
            )
        root = selection.root.resolve(strict=False)
        target = root.joinpath(*self.parts).resolve(strict=False)
        if target != root and not target.is_relative_to(root):
            raise ValueError(
                f"workspace reference resolves outside selected workspace: {target}"
            )
        return target


def looks_like_workspace_reference(value: object) -> bool:
    """Return whether a stored value declares the workspace URI scheme."""
    return isinstance(value, str) and value.lower().startswith(f"{_SCHEME}:")


def reference_leaf(value: object) -> str | None:
    """Return a logical reference's final decoded segment; None for any other value."""
    if not looks_like_workspace_reference(value):
        return None
    try:
        parts = WorkspaceReference.parse(str(value)).parts
    except ValueError:
        return None
    return parts[-1] if parts else None


def resolve_registered_path(external_id: object, stored_path: object) -> str | None:
    """Return the current filesystem path for a registered artifact identity.

    A ``workspace://`` identity is the relocatable truth: it resolves against
    the workspace root this process selected, so a row registered under a
    previous root still reaches the live file. An identity belonging to another
    workspace, or one that escapes the selected root, raises ``ValueError``
    rather than silently falling back to the stale recorded path. Every other
    identity — a repository artifact or a pre-logical absolute row — keeps its
    stored path unchanged.
    """
    if not looks_like_workspace_reference(external_id):
        return str(stored_path) if stored_path else None
    from infra import config

    selection = workspace_selection_for_root(config.WORKSPACE.resolve())
    return str(WorkspaceReference.parse(str(external_id)).resolve(selection))


def workspace_selection_for_root(root: str | Path) -> WorkspaceSelection:
    """Return the process selection after verifying its root matches ``root``."""
    from infra.config import WORKSPACE_SELECTION

    resolved = Path(root).expanduser().resolve(strict=False)
    if WORKSPACE_SELECTION.root != resolved:
        raise ValueError(
            "logical workspace reference cannot resolve against a different "
            f"selected root: got {resolved}, selected root is "
            f"{WORKSPACE_SELECTION.root}"
        )
    return WORKSPACE_SELECTION


def _decode_part(encoded: str) -> str:
    """Decode one canonical URI segment as strict UTF-8."""
    if "%" in _PERCENT_ESCAPE.sub("", encoded):
        raise ValueError("workspace reference contains an invalid percent escape")
    try:
        return unquote_to_bytes(encoded).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("workspace reference contains invalid UTF-8") from exc


def _validate_parts(parts: tuple[str, ...]) -> None:
    """Reject traversal, separators, empty segments, and null bytes."""
    for part in parts:
        if not isinstance(part, str) or not part:
            raise ValueError("workspace reference contains an empty path segment")
        if part in {".", ".."}:
            raise ValueError("workspace reference contains path traversal")
        if "/" in part or "\\" in part:
            raise ValueError("workspace reference contains an encoded or raw separator")
        if "\x00" in part:
            raise ValueError("workspace reference contains a null byte")
