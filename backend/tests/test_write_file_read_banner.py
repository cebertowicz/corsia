"""A whole write does not keep a copy of the line a read puts in front of a shared document."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re

import pytest

from registry.doc_freshness import (
    READ_BANNER_OPEN, format_freshness, read_banner, strip_read_banner, verification_hint,
)
from tests.test_shared_document_freshness import _register, freshness_scope  # noqa: F401

_NOW = datetime(2026, 11, 28, tzinfo=timezone.utc)
_ARTIFACT = "39ae24e4-ef2"


def _every_banner_a_read_can_print() -> list[str]:
    """One banner per freshness form, as the read builds it: each state, a day and several days, any owner and date."""
    states = [
        {"state": "archived"},
        {"state": "as_of", "verified_at": "2026-10-06"},
        {"state": "as_of", "verified_at": "2031-01-31"},
        {"state": "never_verified"},
        {"state": "current", "age_days": 1},
        {"state": "current", "age_days": 94},
        {"state": "draft_expiring", "age_days": 0},
        {"state": "overdue", "age_days": 1},
        {"state": "overdue", "age_days": 120},
    ]
    owners = ["Documents & Artifacts", "conductor", "Providers & Inference (9498610f)", "Sprint & Release"]
    return [
        read_banner(format_freshness(state, owner), verification_hint(state, _ARTIFACT))
        for state in states for owner in owners
    ]


def test_every_form_the_read_prints_is_dropped_with_one_blank_line_after_it():
    banners = _every_banner_a_read_can_print()
    assert len(banners) == 36 and len(set(banners)) == len(banners)
    for banner in banners:
        assert banner.startswith(READ_BANNER_OPEN)
        assert strip_read_banner(f"{banner}\n# Title\n\nBody.\n") == "# Title\n\nBody.\n", banner
        assert strip_read_banner(f"{banner}\n\n# Title\n") == "# Title\n", banner
        assert strip_read_banner(f"{banner}\r\n\r\n# Title\r\n") == "# Title\r\n", banner
        assert strip_read_banner(banner) == "", banner
    # Only the one blank line goes; a second blank line after it is the document's own.
    assert strip_read_banner(f"{banners[0]}\n\n\n# Title\n") == "\n# Title\n"


@pytest.mark.parametrize("imitation", [
    "[Shared document · Owner: Structured Plans · as of 2026-11-24 · built, waiting on your look before merge]",
    "[Shared document · Owner: Providers & Inference · status: round 14 draft for parent inspection · 2026-10-23]",
    "[Shared document · Owner: Structured Plans · as of 2026-11-24 18:40 PST]",
    "[Shared document · Owner: X · status: round 14 draft · never verified]",
    "[Shared document · Owner: X · waiting on a review · as of 2026-11-24]",
    "[Shared document · Owner: X · round 14 draft · verified 3 days ago · review overdue]",
    "[Shared document · Owner: X · note · as of 2026-11-24 · review overdue]",
    "[Shared document · Owner: X · as of 2026-11-24] and a note after it",
    "[Shared document · Owner: X · verified 3 days ago] Its owner clears this a different way.",
    "[Shared document · as of 2026-11-24]",
    " [Shared document · Owner: X · as of 2026-11-24]",
    "[shared document · Owner: X · as of 2026-11-24]",
])
def test_a_first_line_the_read_could_not_have_printed_is_kept_byte_for_byte(imitation):
    text = f"{imitation}\n\n# Title\n\nBody.\n"
    assert strip_read_banner(text) == text


def test_a_banner_after_line_one_is_kept():
    banner = read_banner(format_freshness({"state": "as_of", "verified_at": "2026-10-06"}, "conductor"), None)
    for text in (f"# Title\n{banner}\nBody.\n", f"\n{banner}\n# Title\n", f"# Title\n\n{banner}\n"):
        assert strip_read_banner(text) == text


def test_text_without_a_banner_and_empty_text_are_unchanged():
    for text in ("", "\n", "# Title\n", "[Shared document]\n", "[Shared document · \n"):
        assert strip_read_banner(text) == text


def _numbers_removed(read_output: str) -> str:
    """What an agent that takes the numbers off the read's lines writes back."""
    return "\n".join(re.sub(r"^\s+\d+\t", "", line) for line in read_output.splitlines()) + "\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("numbers", ["removed", "kept"])
async def test_write_file_of_a_whole_read_leaves_no_banner_in_a_registered_shared_document(freshness_scope, numbers):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool

    scope = freshness_scope
    path, _artifact = _register(scope, "copy.md", "# Title\n\nBody line.\n")
    workspace = scope["workspace"]
    read = await ReadFileTool(workspace=workspace, allowed_dir=workspace).execute(
        str(path), _thread_id=scope["owner"],
    )
    assert read.startswith(READ_BANNER_OPEN)
    copied = _numbers_removed(read) if numbers == "removed" else read

    receipt = await WriteFileTool(workspace=workspace, allowed_dir=workspace).execute(
        str(path), copied, _thread_id=scope["owner"], _thread_project_id="project-alpha",
    )

    written = path.read_text(encoding="utf-8")
    assert not written.startswith(READ_BANNER_OPEN)
    assert "Shared document ·" not in written
    expected = "# Title\n\nBody line.\n" if numbers == "removed" else "     1\t# Title\n     2\t\n     3\tBody line."
    assert written == expected
    # The receipt is the registry's own JSON, as for any managed write, and counts the bytes written.
    assert json.loads(receipt)["registry_managed"] is True


@pytest.mark.asyncio
async def test_write_file_keeps_an_imitation_and_a_later_banner_in_a_registered_shared_document(freshness_scope):
    from tools.filesystem_rw import WriteFileTool

    scope = freshness_scope
    path, _artifact = _register(scope, "kept.md", "# Old\n")
    workspace = scope["workspace"]
    tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
    context = {"_thread_id": scope["owner"], "_thread_project_id": "project-alpha"}
    imitation = "[Shared document · Owner: Documents & Artifacts · as of 2026-11-24 · waiting on a review]\n\n# New\n"
    later = "# New\n\n[Shared document · Owner: Documents & Artifacts · as of 2026-11-24]\nBody.\n"

    await tool.execute(str(path), imitation, **context)
    assert path.read_text(encoding="utf-8") == imitation
    await tool.execute(str(path), later, **context)
    assert path.read_text(encoding="utf-8") == later


@pytest.mark.asyncio
async def test_write_file_drops_a_copied_banner_from_a_new_file_in_a_thread_folder(freshness_scope):
    from tools.filesystem_rw import WriteFileTool

    scope = freshness_scope
    workspace = scope["workspace"]
    path = workspace / "projects" / "alpha" / ".threads" / scope["owner"] / "notes" / "new.md"
    banner = read_banner(format_freshness({"state": "never_verified"}, "conductor"), verification_hint({"state": "never_verified"}, _ARTIFACT))

    await WriteFileTool(workspace=workspace, allowed_dir=workspace).execute(
        str(path), f"{banner}\n# New\n", _thread_id=scope["owner"], _thread_project_id="project-alpha",
    )

    assert path.read_text(encoding="utf-8") == "# New\n"
