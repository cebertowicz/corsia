
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from PIL import Image  # noqa: E402
from core.context_builder import ContextBuilder  # noqa: E402


def _make_ctx():
    import tempfile
    ws = Path(tempfile.mkdtemp())
    return ContextBuilder(ws)


def _make_temp_image(width, height, color="red", fmt="PNG"):
    img = Image.new("RGB", (width, height), color=color)
    fd, path = tempfile.mkstemp(suffix=f".{fmt.lower()}")
    os.close(fd)
    img.save(path, format=fmt)
    return path


class TestProviderResolution:

    def test_anthropic_model_applies_anthropic_limits(self):
        ctx = _make_ctx()

        path = _make_temp_image(100, 100)
        try:
            content = ctx._build_user_content(
                "test message", [path], model="anthropic/claude-sonnet-4-6"
            )
            assert isinstance(content, list)
            img_block = next(b for b in content if b.get("type") == "image_url")
            assert img_block is not None
        finally:
            os.unlink(path)

    def test_openai_model_applies_openai_limits(self):
        ctx = _make_ctx()

        path = _make_temp_image(100, 100)
        try:
            content = ctx._build_user_content(
                "test message", [path], model="openai/gpt-4o"
            )
            assert isinstance(content, list)
        finally:
            os.unlink(path)

    def test_local_model_no_resize(self):
        ctx = _make_ctx()

        path = _make_temp_image(100, 100)
        try:
            content = ctx._build_user_content(
                "test message", [path], model="lmstudio/qwen2.5-72b"
            )
            assert isinstance(content, list)
        finally:
            os.unlink(path)

    def test_omlx_model_no_resize(self):
        ctx = _make_ctx()

        path = _make_temp_image(100, 100)
        try:
            content = ctx._build_user_content(
                "test message", [path], model="omlx@ns-abc123/qwen3"
            )
            assert isinstance(content, list)
        finally:
            os.unlink(path)


class TestImagePassthrough:

    def test_small_png_passthrough(self):
        ctx = _make_ctx()

        path = _make_temp_image(100, 100)
        try:
            content = ctx._build_user_content(
                "test message", [path], model="anthropic/claude-sonnet-4-6"
            )
            assert isinstance(content, list)

            img_block = next(b for b in content if b.get("type") == "image_url")
            assert "url" in img_block["image_url"]
            url = img_block["image_url"]["url"]
            assert url.startswith("data:image/png;base64,")

            text_block = next(b for b in content if b.get("type") == "text")
            assert text_block["text"] == "test message"
        finally:
            os.unlink(path)

    def test_no_media_returns_text(self):
        ctx = _make_ctx()

        content = ctx._build_user_content("hello world", None, model="anthropic/claude-sonnet-4-6")
        assert content == "hello world"

    def test_empty_media_returns_text(self):
        ctx = _make_ctx()

        content = ctx._build_user_content("hello world", [], model="anthropic/claude-sonnet-4-6")
        assert content == "hello world"


class TestErrorHandling:

    def test_corrupt_image_raises_runtime_error(self):
        ctx = _make_ctx()

        fd, path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"not an image at all")
        os.close(fd)

        try:
            content = ctx._build_user_content(
                "test message", [path], model="anthropic/claude-sonnet-4-6"
            )
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "anthropic" in str(e).lower() or "image" in str(e).lower()
        finally:
            os.unlink(path)

    def test_non_image_attachment_skipped(self):
        ctx = _make_ctx()

        fd, path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"this is a text file pretending to be an image")
        os.close(fd)

        try:
            content = ctx._build_user_content(
                "test message", [path], model="anthropic/claude-sonnet-4-6"
            )
            assert content == "test message"
        finally:
            os.unlink(path)


class TestMultipleImages:

    def test_request_total_is_passed_to_every_image_resize(self, monkeypatch):
        """Every image uses the complete provider-request count."""
        observed = []

        def capture(data, mime_type, limits, image_count=1):
            observed.append(image_count)
            return data, mime_type

        monkeypatch.setattr(
            "core.image_resize.resize_image_for_provider",
            capture,
        )
        ctx = _make_ctx()
        path = _make_temp_image(1, 1)
        try:
            content = ctx._build_user_content(
                "many images",
                [path] * 21,
                model="anthropic/claude-sonnet-4-6",
            )
        finally:
            os.unlink(path)

        assert isinstance(content, list)
        assert observed == [21] * 21

    def test_multiple_images_all_included(self):
        ctx = _make_ctx()

        path1 = _make_temp_image(100, 100)
        path2 = _make_temp_image(200, 200, color="blue")

        try:
            content = ctx._build_user_content(
                "two images", [path1, path2], model="anthropic/claude-sonnet-4-6"
            )
            assert isinstance(content, list)

            img_blocks = [b for b in content if b.get("type") == "image_url"]
            text_blocks = [b for b in content if b.get("type") == "text"]

            assert len(img_blocks) == 2
            assert len(text_blocks) == 1
            assert text_blocks[0]["text"] == "two images"
        finally:
            os.unlink(path1)
            os.unlink(path2)
