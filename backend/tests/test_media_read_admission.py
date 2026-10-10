"""Image admission contracts for media-aware read_file."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image


def _png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="red").save(stream, format="PNG")
    return stream.getvalue()


def _ppm_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(stream, format="PPM")
    return stream.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """Return an isolated filesystem-tool workspace."""
    from tools import sandbox_config

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [root])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(
        sandbox_config, "_list_authoritative_repo_policies", lambda: []
    )
    return root


def _write(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.write_bytes(data)
    return path


def _sparse(root: Path, name: str, header: bytes, total: int) -> Path:
    path = root / name
    with path.open("wb") as handle:
        handle.write(header)
        handle.truncate(total)
    return path


async def _read(root: Path, path: Path):
    from tools.filesystem_rw import ReadFileTool

    return await ReadFileTool(workspace=root).execute(path=path.name)


@pytest.mark.asyncio
async def test_image_read_returns_placeholder_carrying_reference_metadata(
    workspace,
) -> None:
    """An image read yields short text plus an immutable media reference."""
    from core.media_reads import MediaToolResult

    data = _png_bytes()
    path = _write(workspace, "shot.png", data)

    result = await _read(workspace, path)

    assert isinstance(result, MediaToolResult)
    assert result.media.mime_type == "image/png"
    assert result.media.kind == "image"
    assert result.media.byte_size == len(data)
    assert result.media.path == str(path)
    assert "shot.png" in result
    assert len(result) < 500
    assert base64.b64encode(data).decode()[:32] not in result


@pytest.mark.asyncio
async def test_image_read_recognizes_pillow_container_without_prefix_signature(
    workspace,
) -> None:
    """Filesystem reads share delivery's bytes-authoritative image identity."""
    from core.media_reads import MediaToolResult

    data = _ppm_bytes()
    path = _write(workspace, "scan.ppm", data)

    result = await _read(workspace, path)

    assert isinstance(result, MediaToolResult)
    assert result.media.kind == "image"
    assert result.media.mime_type == "image/x-portable-pixmap"
    assert result.media.byte_size == len(data)


@pytest.mark.asyncio
async def test_image_read_reference_survives_registry_sanitization(
    workspace, monkeypatch
) -> None:
    """The registry boundary must not flatten the reference into plain text."""
    from core.media_reads import MediaToolResult
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry

    path = _write(workspace, "shot.png", _png_bytes())
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=workspace))

    async def _no_thread_context(self):
        return {"id": "thread-1", "project_id": None}, None

    monkeypatch.setattr(
        ToolRegistry, "_load_thread_context", _no_thread_context, raising=True
    )
    result = await registry.execute("read_file", {"path": path.name})

    assert isinstance(result, MediaToolResult)
    assert result.media.mime_type == "image/png"


@pytest.mark.asyncio
async def test_media_read_runs_off_the_event_loop(workspace, monkeypatch) -> None:
    """Digesting media must not block the loop; the read is offloaded."""
    import threading

    from core import media_reads

    observed: dict[str, int] = {}
    original = media_reads.read_media_result

    def _record(handle, file_stat, path):
        observed["thread"] = threading.get_ident()
        return original(handle, file_stat, path)

    monkeypatch.setattr(media_reads, "read_media_result", _record)
    path = _write(workspace, "shot.png", _png_bytes())

    result = await _read(workspace, path)

    assert isinstance(result, media_reads.MediaToolResult)
    assert observed["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_image_read_rejects_file_above_fifty_megabytes(workspace) -> None:
    """Images are admitted up to 50MB; a larger read declines explicitly."""
    path = _sparse(workspace, "huge.png", _png_bytes(), 50 * 1024 * 1024 + 1)

    result = await _read(workspace, path)

    assert result.startswith("Error")
    assert "50MB" in result
    assert "image" in result.lower()
    assert "Max readable size is 10MB" not in result


@pytest.mark.asyncio
async def test_image_read_admits_file_just_below_fifty_megabytes(workspace) -> None:
    """The 50MB image ceiling is inclusive, not a smaller inherited limit."""
    from core.media_reads import MediaToolResult

    path = _sparse(workspace, "big.png", _png_bytes(), 50 * 1024 * 1024)

    result = await _read(workspace, path)

    assert isinstance(result, MediaToolResult)
    assert result.media.byte_size == 50 * 1024 * 1024
