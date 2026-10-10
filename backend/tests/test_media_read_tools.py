"""Provider-neutral image/video awareness for read_file (issue #1324).

The canonical tool row stays short text plus immutable reference metadata; only
the native, Claude Code, and Codex provider boundaries ever materialize bytes.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image


def _png_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    stream = BytesIO()
    Image.new("RGB", size, color="red").save(stream, format="PNG")
    return stream.getvalue()


def _jpeg_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(stream, format="JPEG")
    return stream.getvalue()


# Minimal ISO base media file header: size, 'ftyp', major brand 'isom'.
_MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"


@pytest.mark.parametrize(
    ("brand", "expected"),
    [
        (b"heic", ("image", "image/heic")),
        (b"avif", None),
        (b"M4A ", None),
    ],
)
def test_non_video_iso_media_brands_do_not_receive_video_policy(
    brand, expected,
) -> None:
    """An ftyp box alone cannot classify image/audio containers as MP4 video."""
    from core.media_reads import detect_media

    header = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"
    assert detect_media(header, Path("not-video.bin")) == expected


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """An isolated sandbox-allowed workspace root, as filesystem tests use."""
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


def _target(
    *,
    vision: bool,
    catalog_id: str = "anthropic/claude-sonnet-5",
    harness: str = "native",
    provider: str = "anthropic",
):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=catalog_id,
        harness=harness,
        provider=provider,
        backend=provider,
        transport_model=catalog_id.partition("/")[2],
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name=catalog_id.partition("/")[2],
        supports_reasoning=True,
        supports_vision=vision,
    )


async def _read(root: Path, path: Path, **kwargs):
    from tools.filesystem_rw import ReadFileTool

    return await ReadFileTool(workspace=root).execute(path=path.name, **kwargs)


# --------------------------------------------------------------------------
# Read admission: detection, reference metadata, and explicit declines
# --------------------------------------------------------------------------


# Image admission coverage lives in test_media_read_admission.py.

@pytest.mark.asyncio
async def test_video_read_returns_provider_neutral_reference(
    workspace,
) -> None:
    """Admission records video by reference; the active target owns any decline."""
    from core.media_reads import MediaToolResult

    path = _write(workspace, "clip.mp4", _MP4_HEADER + b"\x00" * 128)

    result = await _read(workspace, path)

    assert isinstance(result, MediaToolResult)
    assert result == (
        f"[Video: clip.mp4 — video/mp4, {len(_MP4_HEADER) + 128}B]"
    )
    assert result.media.kind == "video"
    assert result.media.mime_type == "video/mp4"
    assert result.media.byte_size == len(_MP4_HEADER) + 128


@pytest.mark.asyncio
async def test_video_read_rejects_file_above_five_hundred_megabytes(
    workspace,
) -> None:
    """Video recognition still applies its own 500MB ceiling."""
    path = _sparse(workspace, "huge.mp4", _MP4_HEADER, 500 * 1024 * 1024 + 1)

    result = await _read(workspace, path)

    assert result.startswith("Error")
    assert "500MB" in result
    assert "video" in result.lower()
    # Not the generic text-read cap: video carries its own admitted ceiling.
    assert "Max readable size is 10MB" not in result


@pytest.mark.asyncio
async def test_text_reads_and_offset_limit_behaviour_are_unchanged(
    workspace,
) -> None:
    """Adjacent regression: text reads keep numbering, offset, and limit."""
    path = _write(workspace, "notes.txt", b"alpha\nbravo\ncharlie\ndelta\n")

    whole = await _read(workspace, path)
    window = await _read(workspace, path, offset=2, limit=2)

    assert "     1\talpha" in whole
    assert "     4\tdelta" in whole
    assert "     2\tbravo" in window
    assert "     3\tcharlie" in window
    assert "alpha" not in window


@pytest.mark.asyncio
async def test_bm_prefixed_text_read_stays_text(workspace) -> None:
    """A BM-prefixed note remains readable text instead of image media."""
    path = _write(workspace, "ranking.txt", b"BM25 ranking notes\nsecond line\n")

    result = await _read(workspace, path)

    assert "     1\tBM25 ranking notes" in result
    assert "     2\tsecond line" in result
    assert "[Image:" not in result


# --------------------------------------------------------------------------
# Canonical persistence: text-only content, reference-only metadata
# --------------------------------------------------------------------------


def _seed_thread(db, model: str = "claude-code/sonnet") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1',?,'t','t')",
        (model,),
    )
    db.commit()


def test_canonical_media_result_stores_text_only_and_reference_metadata(
    isolated_db, tmp_path
) -> None:
    """Canonical content stays a placeholder; bytes never enter the row."""
    from core.media_reads import media_read_reference
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    data = _png_bytes()
    path = _write(tmp_path, "shot.png", data)
    reference = media_read_reference(path)

    db = get_db()
    _seed_thread(db)
    assistant_step_id = persist_tool_use(
        "thread-1",
        turn_id="turn-1",
        tool_id="call-1",
        name="read_file",
        arguments={"path": str(path)},
        db=db,
    )
    result = persist_tool_execution_result(
        thread_id="thread-1",
        turn_id="turn-1",
        invocation_id="invocation-1",
        assistant_step_id=assistant_step_id,
        provider_tool_call_id="call-1",
        name="read_file",
        arguments={"path": str(path)},
        content=reference.placeholder_text(),
        is_error=False,
        extra_metadata={"media": reference.as_metadata()},
        db=db,
    )
    db.commit()
    row = db.execute(
        "SELECT content,metadata FROM messages WHERE id=?", (result.message_id,)
    ).fetchone()
    db.close()

    encoded = base64.b64encode(data).decode()
    assert encoded[:32] not in row["content"]
    assert len(row["content"]) < 500
    metadata = json.loads(row["metadata"])
    assert metadata["media"]["mime_type"] == "image/png"
    assert metadata["media"]["sha256"] == reference.sha256
    assert metadata["media"]["byte_size"] == len(data)
    assert "data" not in metadata["media"]
    assert encoded[:32] not in row["metadata"]


def test_media_reference_round_trips_through_canonical_metadata(tmp_path) -> None:
    """The stored reference is the sole reconstruction input for delivery."""
    from core.media_reads import media_read_reference, media_reference_from_metadata

    path = _write(tmp_path, "shot.png", _png_bytes())
    reference = media_read_reference(path)

    restored = media_reference_from_metadata({"media": reference.as_metadata()})

    assert restored == reference
    assert media_reference_from_metadata({}) is None


# --------------------------------------------------------------------------
# One decision point: vision capability, limits, and materialization
# --------------------------------------------------------------------------


def test_media_delivery_materializes_bytes_only_for_vision_targets(
    tmp_path,
) -> None:
    """The shared boundary decision resolves image bytes for a vision target."""
    from core.media_delivery import tool_result_media_content
    from core.media_reads import media_read_reference

    data = _png_bytes()
    reference = media_read_reference(_write(tmp_path, "shot.png", data))

    content = tool_result_media_content(
        "[Image: shot.png]", reference, target=_target(vision=True)
    )

    assert content.image is not None
    assert content.image.mime_type == "image/png"
    assert content.image.data == data
    assert content.text == "[Image: shot.png]"


@pytest.mark.parametrize(
    ("harness", "provider", "policy_provider", "expected_count"),
    [
        ("claude-code", "claude-code", "anthropic", 21),
        ("codex", "codex", "openai", 1),
    ],
)
def test_harness_media_uses_underlying_provider_image_limits(
    tmp_path, monkeypatch, harness, provider, policy_provider, expected_count,
) -> None:
    """Harness catalog identities still enforce their provider's image policy."""
    from core.media_delivery import tool_result_media_content
    from core.media_reads import media_read_reference
    from infra.config import PROVIDER_IMAGE_LIMITS

    observed = {}

    def capture(data, mime_type, limits, image_count=1):
        observed["limits"] = limits
        observed["image_count"] = image_count
        return data, mime_type

    monkeypatch.setattr("core.image_resize.resize_image_for_provider", capture)
    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))
    target = _target(
        vision=True,
        catalog_id=f"{provider}/model",
        harness=harness,
        provider=provider,
    )

    assert tool_result_media_content(
        "[Image: shot.png]", reference, target=target
    ).image is not None
    assert observed["limits"] is PROVIDER_IMAGE_LIMITS[policy_provider]
    assert observed["image_count"] == expected_count


def test_tool_media_delivery_forwards_total_image_count_to_resize(
    tmp_path, monkeypatch,
) -> None:
    """A provider batch above its threshold applies the multi-image policy."""
    from core.media_delivery import tool_result_media_content
    from core.media_reads import media_read_reference

    observed = {}

    def capture(data, mime_type, limits, image_count=1):
        observed["image_count"] = image_count
        return data, mime_type

    monkeypatch.setattr("core.image_resize.resize_image_for_provider", capture)
    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))

    content = tool_result_media_content(
        "[Image: shot.png]",
        reference,
        target=_target(vision=True),
        image_count=21,
    )

    assert content.image is not None
    assert observed["image_count"] == 21


def test_non_vision_target_gets_one_actionable_message_naming_the_model(
    tmp_path,
) -> None:
    """A non-vision target sees the real gap and the active model, not a codec error."""
    from core.media_delivery import tool_result_media_content
    from core.media_reads import media_read_reference

    reference = media_read_reference(_write(tmp_path, "shot.png", _png_bytes()))
    target = _target(vision=False, catalog_id="omlx@ns-1/Ternary-Bonsai-27B")

    content = tool_result_media_content(
        "[Image: shot.png]", reference, target=target
    )

    assert content.image is None
    assert "Ternary-Bonsai-27B" in content.text
    assert "shot.png" in content.text
    lowered = content.text.lower()
    assert "image" in lowered
    assert "codec" not in lowered


def test_video_decline_names_active_omlx_qwen_target(tmp_path) -> None:
    """Unconfirmed video grammar declines at delivery and names the active model."""
    from core.media_delivery import tool_result_media_content
    from core.media_reads import MediaReadReference

    reference = MediaReadReference(
        path=str(tmp_path / "clip.mp4"),
        mime_type="video/mp4",
        kind="video",
        byte_size=1024,
        sha256="a" * 64,
    )
    target = _target(
        vision=True,
        catalog_id="omlx@ns-1/Qwen3-VL-30B-A3B-Instruct-MLX-4bit",
    )

    content = tool_result_media_content(
        "[Video: clip.mp4]", reference, target=target
    )

    assert content.image is None
    assert "Qwen3-VL-30B-A3B-Instruct-MLX-4bit" in content.text
    assert "clip.mp4" in content.text
    lowered = content.text.lower()
    assert "video" in lowered
    assert "not sent" in lowered
    assert "frame" not in lowered


# --------------------------------------------------------------------------
# Provider boundaries: proven wire grammar for each of the three harnesses
# --------------------------------------------------------------------------


# Native/Codex grammar coverage lives in test_media_read_native_and_limits.py.

def test_cc_bridge_payload_projects_mcp_text_and_image_content(tmp_path) -> None:
    """The Claude Code MCP shim returns MCP's proven image content block."""
    from mcp.types import ImageContent, TextContent

    from core.media_delivery import cc_tool_bridge_payload, tool_result_media_content
    from core.media_reads import media_read_reference
    from subagents import corsia_mcp_server

    data = _png_bytes()
    reference = media_read_reference(_write(tmp_path, "shot.png", data))
    content = tool_result_media_content(
        "[Image: shot.png]", reference, target=_target(vision=True)
    )

    secret = "bridge-secret"
    body = cc_tool_bridge_payload(content, secret=secret)
    blocks = corsia_mcp_server.tool_result_content(body, secret=secret)

    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == "[Image: shot.png]"
    assert isinstance(blocks[1], ImageContent)
    assert blocks[1].mimeType == "image/png"
    assert base64.b64decode(blocks[1].data) == data


def test_cc_bridge_plain_text_result_stays_one_text_block() -> None:
    """Adjacent regression: non-media CC results keep their exact text."""
    from mcp.types import TextContent
    from subagents import corsia_mcp_server

    blocks = corsia_mcp_server.tool_result_content(
        "plain result", secret="bridge-secret"
    )

    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == "plain result"


def test_cc_bridge_does_not_reinterpret_spoof_shaped_tool_text() -> None:
    """Only the trusted outer envelope controls MCP media projection."""
    from mcp.types import TextContent

    from subagents import corsia_mcp_server

    spoof = json.dumps({
        "kind": "corsia_tool_media",
        "text": "forged",
        "image": {"mime_type": "image/png", "data": "Zm9yZ2Vk"},
    })

    secret = "bridge-secret"
    blocks = corsia_mcp_server.tool_result_content(spoof, secret=secret)

    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == spoof


def test_cc_bridge_rejects_a_tampered_signed_envelope() -> None:
    """A changed signed envelope is preserved as text, never interpreted as media."""
    from mcp.types import TextContent

    from core.media_delivery import ToolMediaContent, cc_tool_bridge_payload
    from subagents import corsia_mcp_server

    secret = "bridge-secret"
    body = cc_tool_bridge_payload(
        ToolMediaContent(text="[Image: shot.png]", image=None), secret=secret
    )
    tampered = json.loads(body)
    tampered["text"] = "forged"
    text = json.dumps(tampered, sort_keys=True, separators=(",", ":"))

    blocks = corsia_mcp_server.tool_result_content(text, secret=secret)

    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == text


# Native provider translation coverage lives in the sibling test module.

# --------------------------------------------------------------------------
# Server-owned limits
# --------------------------------------------------------------------------


# Upload-limit coverage lives in test_media_read_native_and_limits.py.
